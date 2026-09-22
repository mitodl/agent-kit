"""Unit tests for the SessionStart/PostToolUse hook logic in hooks.py.

``indexer.index_path`` and process spawning are monkeypatched throughout:
these tests exercise hooks.py's own orchestration (git-repo check, queueing,
the lock handoff, stdin-JSON parsing, path resolution), not the real indexer
or a real detached drainer. The lock itself is real: a kernel flock.
"""

import os
import subprocess
import sys
import tempfile

import pytest

from witan_code import context, hooks


@pytest.fixture
def _state(tmp_path, monkeypatch):
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    return tmp


@pytest.fixture
def _repo(tmp_path, monkeypatch, _state):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    return hooks.repo_module.root(repo)


@pytest.fixture
def _spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(
        hooks, "popen_detached", lambda argv, **kw: calls.append((argv, kw))
    )
    return calls


@pytest.fixture
def _indexed(monkeypatch):
    calls = []
    monkeypatch.setattr(hooks.indexer, "index_path", lambda p, force: calls.append(p))
    return calls


def _payload(path) -> str:
    return f'{{"tool_input": {{"file_path": "{path}"}}}}'


# ── session_init ──────────────────────────────────────────────────────────────


def test_session_init_noop_outside_git_repo(tmp_path, monkeypatch, _state, _spawned):
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(non_repo))

    hooks.session_init()

    assert _spawned == []


def test_session_init_queues_the_project_and_hands_the_lock_to_a_drainer(
    _repo, _spawned
):
    hooks.session_init()

    assert len(_spawned) == 1
    argv, kwargs = _spawned[0]
    assert argv[1:5] == ["-m", "witan_code", "_drain-pending", str(_repo)]
    assert kwargs["pass_fds"] == (int(argv[5]),)
    assert hooks._take_pending(_repo) == [_repo]


def test_session_init_in_a_subdirectory_shares_the_toplevel_s_lock(
    _repo, monkeypatch, _spawned
):
    """A session started in a monorepo subdirectory must not get a lock of its
    own: its full index and the edits' drainer write the same branch view."""
    sub = _repo / "services" / "api"
    sub.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(sub))
    held = hooks._try_lock(_repo)

    hooks.session_init()

    os.close(held)
    assert _spawned == []
    assert hooks._take_pending(_repo) == [sub]  # queued, not dropped


def test_session_init_survives_a_failed_spawn(_repo, monkeypatch):
    def _boom(*a, **k):
        raise OSError("cannot spawn")

    monkeypatch.setattr(hooks, "popen_detached", _boom)

    hooks.session_init()

    fd = hooks._try_lock(_repo)
    assert fd is not None  # released for the next hook
    os.close(fd)
    assert hooks._has_pending(_repo)  # and the refresh is still queued


# ── reindex_hook outside a repo ───────────────────────────────────────────────


def test_reindex_hook_noop_on_empty_payload(_indexed):
    hooks.reindex_hook("")
    assert _indexed == []


def test_reindex_hook_noop_on_malformed_json(_indexed):
    hooks.reindex_hook("not json")
    assert _indexed == []


def test_reindex_hook_noop_without_tool_input(_indexed):
    hooks.reindex_hook('{"tool_name": "Edit"}')
    assert _indexed == []


def test_reindex_hook_noop_when_file_missing(tmp_path, _indexed):
    hooks.reindex_hook(_payload(tmp_path / "does-not-exist.py"))
    assert _indexed == []


def test_reindex_hook_outside_a_repo_indexes_in_the_foreground(tmp_path, _indexed):
    target = tmp_path / "a.py"
    target.write_text("def f(): pass")

    hooks.reindex_hook(_payload(target))

    assert _indexed == [target]


def test_reindex_hook_resolves_relative_path_against_cwd(
    tmp_path, monkeypatch, _indexed
):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "b.py"
    target.write_text("def g(): pass")

    hooks.reindex_hook('{"tool_input": {"path": "b.py"}}')

    assert _indexed == [target]


def test_reindex_hook_swallows_index_failure(tmp_path, monkeypatch):
    target = tmp_path / "c.py"
    target.write_text("def h(): pass")

    def _boom(p, force):
        raise RuntimeError("parse error")

    monkeypatch.setattr(hooks.indexer, "index_path", _boom)

    hooks.reindex_hook(
        f'{{"tool_input": {{"filename": "{target}"}}}}'
    )  # must not raise


# ── reindex_hook inside a repo ────────────────────────────────────────────────


def test_reindex_hook_in_a_repo_queues_and_spawns_a_drainer(_repo, _spawned, _indexed):
    target = _repo / "a.py"
    target.write_text("def f(): pass")

    hooks.reindex_hook(_payload(target))

    assert _indexed == []  # the drainer writes, not the hook
    assert [argv[1:5] for argv, _ in _spawned] == [
        ["-m", "witan_code", "_drain-pending", str(_repo)]
    ]
    assert hooks._take_pending(_repo) == [target]


def test_reindex_hook_leaves_the_path_to_a_running_drainer(_repo, _spawned):
    target = _repo / "a.py"
    target.write_text("def f(): pass")
    held = hooks._try_lock(_repo)

    hooks.reindex_hook(_payload(target))

    os.close(held)
    assert _spawned == []
    assert hooks._take_pending(_repo) == [target]


# ── drain_pending ─────────────────────────────────────────────────────────────


def test_drain_pending_indexes_each_queued_target_once_and_releases(_repo, _indexed):
    a, b = _repo / "a.py", _repo / "b.py"
    for path in (a, b):
        path.write_text("x = 1")
    for path in (a, b, a, a):
        hooks._enqueue(_repo, path)

    hooks.drain_pending(_repo, hooks._try_lock(_repo))

    assert _indexed == [a, b]
    assert not hooks._has_pending(_repo)
    assert not context._busy_path(_repo).exists()
    fd = hooks._try_lock(_repo)
    assert fd is not None
    os.close(fd)


def test_drain_pending_picks_up_a_target_queued_while_it_held_the_lock(
    _repo, monkeypatch
):
    a, b = _repo / "a.py", _repo / "b.py"
    for path in (a, b):
        path.write_text("x = 1")
    hooks._enqueue(_repo, a)
    indexed = []

    def _index(p, force):
        indexed.append(p)
        if p == a:
            # An edit landing mid-drain: its hook finds the lock held and
            # leaves the path to this drainer's re-check.
            assert hooks._try_lock(_repo) is None
            hooks._enqueue(_repo, b)

    monkeypatch.setattr(hooks.indexer, "index_path", _index)

    hooks.drain_pending(_repo, hooks._try_lock(_repo))

    assert indexed == [a, b]


def test_drain_pending_marks_the_checkout_busy_while_it_works(_repo, monkeypatch):
    a = _repo / "a.py"
    a.write_text("x = 1")
    hooks._enqueue(_repo, a)
    seen = []
    monkeypatch.setattr(
        hooks.indexer,
        "index_path",
        lambda p, force: seen.append(context.indexing_in_progress()),
    )

    hooks.drain_pending(_repo, hooks._try_lock(_repo))

    assert seen == [True]
    assert context.indexing_in_progress() is False


def test_drain_pending_skips_a_target_deleted_since_it_was_queued(_repo, _indexed):
    hooks._enqueue(_repo, _repo / "gone.py")

    hooks.drain_pending(_repo, hooks._try_lock(_repo))

    assert _indexed == []
    assert not hooks._has_pending(_repo)


def test_drain_pending_continues_past_a_failing_target(_repo, monkeypatch):
    a, b = _repo / "a.py", _repo / "b.py"
    for path in (a, b):
        path.write_text("x = 1")
        hooks._enqueue(_repo, path)
    indexed = []

    def _index(p, force):
        if p == a:
            raise RuntimeError("write authority changed during preparation")
        indexed.append(p)

    monkeypatch.setattr(hooks.indexer, "index_path", _index)

    hooks.drain_pending(_repo, hooks._try_lock(_repo))

    assert indexed == [b]
    fd = hooks._try_lock(_repo)
    assert fd is not None  # released despite the failure
    os.close(fd)


# ── lock and state ────────────────────────────────────────────────────────────


def test_a_killed_holder_does_not_leave_the_checkout_locked(_repo):
    """The reason the lock is a flock: the kernel drops it with the process,
    so nothing has to decide whether a recorded holder is still alive."""
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, os, sys, time; "
            "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT); "
            "fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); "
            "time.sleep(60)",
            str(context._lock_path(_repo)),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout.readline().strip() == "held"
    assert hooks._try_lock(_repo) is None

    holder.kill()
    holder.wait()

    fd = hooks._try_lock(_repo)
    assert fd is not None
    os.close(fd)


def test_state_dir_is_private(_state):
    path = context.state_dir()

    assert path.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_uid == os.getuid()


def test_state_dir_refuses_a_symlink_planted_in_its_place(_state, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (_state / f"witan-code-{os.getuid()}").symlink_to(elsewhere)

    with pytest.raises(PermissionError):
        context.state_dir()


def test_a_path_with_a_newline_is_not_queued(_repo):
    hooks._enqueue(_repo, _repo / "a\nb.py")

    assert not hooks._has_pending(_repo)
