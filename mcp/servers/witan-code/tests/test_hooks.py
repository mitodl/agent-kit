"""Unit tests for the SessionStart/PostToolUse hook logic in hooks.py.

``indexer.index_path`` and process spawning are monkeypatched throughout:
these tests exercise hooks.py's own orchestration (git-repo check, queueing,
the lock handoff, stdin-JSON parsing, path resolution), not the real indexer
or a real detached drainer. The lock itself is real: a kernel flock.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

from witan_code import context, hooks

from .conftest import requires_stack


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
    # Busy from the moment the lock is taken, before the drainer is even up.
    assert context.indexing_in_progress() is True


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
    assert context.indexing_in_progress() is False


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


def test_reindex_hook_noop_when_a_file_it_never_indexed_is_missing(tmp_path, _indexed):
    hooks.reindex_hook(_payload(tmp_path / "does-not-exist.md"))
    assert _indexed == []


def test_reindex_hook_purges_a_missing_source_file(tmp_path, _indexed):
    """Gone from disk, so indexing it as it is now drops its rows."""
    gone = tmp_path / "does-not-exist.py"

    hooks.reindex_hook(_payload(gone))

    assert _indexed == [gone]


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


# ── Codex apply_patch payloads ────────────────────────────────────────────────


def _codex_post_tool_use(cwd, patch: str) -> str:
    """A Codex ``PostToolUse`` payload for its ``apply_patch`` tool.

    Field set from ``PostToolUseCommandInput`` (codex-rs/hooks/src/schema.rs at
    rust-v0.162.1); ``tool_input``/``tool_response`` from
    ``ApplyPatchHandler::post_tool_use_payload``
    (codex-rs/core/src/tools/handlers/apply_patch.rs). Codex's ``Edit|Write``
    matcher selects this tool by alias, but the payload names it
    ``apply_patch`` and carries the patch, not a path.
    """
    return json.dumps(
        {
            "session_id": "019a1b2c-3d4e-7f00-8a9b-0c1d2e3f4a5b",
            "turn_id": "1",
            "transcript_path": None,
            "cwd": str(cwd),
            "hook_event_name": "PostToolUse",
            "model": "gpt-5-codex",
            "permission_mode": "default",
            "tool_name": "apply_patch",
            "tool_input": {"command": patch},
            "tool_response": "Success. Updated the following files:",
            "tool_use_id": "call-apply-patch",
        }
    )


def test_reindex_hook_reads_every_file_a_codex_patch_names(
    tmp_path, monkeypatch, _indexed
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "added.py").write_text("def a(): pass")
    (tmp_path / "updated.py").write_text("def u(): pass")
    (tmp_path / "moved.py").write_text("def m(): pass")
    # Present on disk, but only named inside an added file's content.
    (tmp_path / "forged.py").write_text("def f(): pass")
    patch = (
        "*** Begin Patch\n"
        "  *** Add File: added.py\n"
        "+def a(): pass\n"
        "+*** Add File: forged.py\n"
        "*** Update File: updated.py\n"
        "@@\n"
        "-def u(): return 1\n"
        "+def u(): pass\n"
        "*** Update File: old.py\n"
        "*** Move to: moved.py\n"
        "*** Delete File: deleted.py\n"
        "*** End Patch\n"
    )

    hooks.reindex_hook(_codex_post_tool_use(tmp_path, patch))

    # old.py and deleted.py are gone, so these two calls purge them.
    assert _indexed == [
        tmp_path / "added.py",
        tmp_path / "updated.py",
        tmp_path / "old.py",
        tmp_path / "moved.py",
        tmp_path / "deleted.py",
    ]


def test_reindex_hook_noop_on_a_codex_payload_without_a_patch(tmp_path, _indexed):
    payload = json.loads(_codex_post_tool_use(tmp_path, ""))
    payload["tool_input"] = {"command": None}

    hooks.reindex_hook(json.dumps(payload))

    assert _indexed == []


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


def test_drain_pending_purges_a_target_deleted_since_it_was_queued(_repo, _indexed):
    hooks._enqueue(_repo, _repo / "gone.py")

    hooks.drain_pending(_repo, hooks._try_lock(_repo))

    assert _indexed == [_repo / "gone.py"]
    assert not hooks._has_pending(_repo)


def test_drain_pending_skips_a_deleted_target_it_never_indexed(_repo, _indexed):
    hooks._enqueue(_repo, _repo / "gone.md")

    hooks.drain_pending(_repo, hooks._try_lock(_repo))

    assert _indexed == []


def test_drain_pending_skips_targets_of_a_removed_checkout(_repo, _indexed):
    """git would resolve them against whatever repo encloses the old path."""
    hooks._enqueue(_repo, _repo / "gone.py")
    lock_fd = hooks._try_lock(_repo)
    shutil.rmtree(_repo)

    hooks.drain_pending(_repo, lock_fd)

    assert _indexed == []


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
            (
                "import fcntl, os, sys, time; "
                "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT); "
                "fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); "
                "time.sleep(60)"
            ),
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


def test_state_dir_tightens_an_existing_directory_s_mode(_state):
    path = _state / f"witan-code-{os.getuid()}"
    path.mkdir(mode=0o777)
    path.chmod(0o777)

    assert context.state_dir().stat().st_mode & 0o777 == 0o700


def test_state_dir_refuses_a_symlink_planted_in_its_place(_state, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (_state / f"witan-code-{os.getuid()}").symlink_to(elsewhere)

    with pytest.raises(PermissionError):
        context.state_dir()


def test_sessions_with_different_witan_overrides_get_separate_drainers(
    _repo, monkeypatch
):
    """The drainer indexes with its spawner's environment, so an edit made
    under ``WITAN_TARGET=qa`` must not be applied by a drainer spawned without
    it."""
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    held = hooks._try_lock(_repo)

    monkeypatch.setenv("WITAN_TARGET", "qa")
    other = hooks._try_lock(_repo)

    assert other is not None
    os.close(other)
    os.close(held)


def test_a_path_with_a_newline_is_not_queued(_repo):
    hooks._enqueue(_repo, _repo / "a\nb.py")

    assert not hooks._has_pending(_repo)


# ── End to end: a Codex patch that deletes and renames ───────────────────────


@requires_stack
def test_a_codex_patch_that_deletes_and_renames_leaves_neither_old_path(
    tmp_path, monkeypatch, _state, _spawned
):
    """The hook queues, the drainer applies, and the graph ends up matching
    the disk: nothing left at the deleted path or at the rename's source."""
    from witan_code import config as cfg_mod
    from witan_code import indexer
    from witan_code import store as store_mod
    from witan_code.graph import OmnigraphClient

    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/cg")
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    src = tmp_path / "repo"
    (src / "pkg").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=src, check=True)
    (src / "keep.py").write_text("def keep():\n    return 1\n")
    (src / "deleted.py").write_text("def deleted():\n    return 1\n")
    (src / "pkg" / "old.py").write_text("def renamed():\n    return 1\n")
    cfg = cfg_mod.load()
    indexer.index_path(src, config=cfg)
    client = OmnigraphClient(
        str(store_mod.store_for_repo("https://github.com/test/cg", cfg)),
        cfg.queries_dir,
    )

    def _files() -> set[str]:
        rows = client.read("code_read.gq", "all_file_hashes", {})
        return {r["slug"].split("#", 1)[1] for r in rows}

    assert _files() == {"keep.py", "deleted.py", "pkg/old.py"}

    # What Codex's apply_patch leaves on disk; the hook runs after it. The
    # rename empties pkg/, so the directory goes too.
    (src / "deleted.py").unlink()
    (src / "new.py").write_text((src / "pkg" / "old.py").read_text())
    (src / "pkg" / "old.py").unlink()
    (src / "pkg").rmdir()
    monkeypatch.chdir(src)
    patch = (
        "*** Begin Patch\n"
        "*** Delete File: deleted.py\n"
        "*** Update File: pkg/old.py\n"
        "*** Move to: new.py\n"
        "*** End Patch\n"
    )

    hooks.reindex_hook(_codex_post_tool_use(src, patch))
    assert _spawned  # stubbed, so the drainer runs in-process below
    checkout = hooks.repo_module.root(src)
    hooks.drain_pending(checkout, hooks._try_lock(checkout))

    assert _files() == {"keep.py", "new.py"}
    renamed = client.read("code_read.gq", "find_by_name", {"name": "renamed"})
    assert [r["file_id"].split("#", 1)[1] for r in renamed] == ["new.py"]
    assert client.read("code_read.gq", "find_by_name", {"name": "deleted"}) == []
