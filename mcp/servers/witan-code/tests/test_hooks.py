"""Unit tests for the SessionStart/PostToolUse hook logic in hooks.py.

``indexer.index_path`` and process spawning are monkeypatched throughout:
these tests exercise hooks.py's own orchestration (git-repo check, lock
acquire/release, stdin-JSON parsing, path resolution), not the real indexer
or a real detached child.
"""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from witan_code import hooks


@pytest.fixture
def _repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    return repo


# ── session_init ──────────────────────────────────────────────────────────────


def test_session_init_noop_outside_git_repo(tmp_path, monkeypatch):
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(non_repo))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()

    calls = []
    monkeypatch.setattr(hooks, "popen_detached", lambda *a, **k: calls.append(a))

    hooks.session_init()

    assert calls == []


def test_session_init_spawns_detached_child_and_holds_lock(_repo, monkeypatch):
    calls = []

    def _spawn(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(pid=os.getpid())

    monkeypatch.setattr(hooks, "popen_detached", _spawn)

    hooks.session_init()

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[1:4] == ["-m", "witan_code", "_index-and-unlock"]
    assert argv[4] == str(_repo)
    lock_arg = argv[5]
    assert Path(lock_arg).is_dir()  # lock held for the (fake) detached child


def test_session_init_skips_when_already_locked(_repo, monkeypatch):
    from witan_code.context import _lock_path

    _lock_path(_repo).mkdir(parents=True)

    calls = []
    monkeypatch.setattr(hooks, "popen_detached", lambda *a, **k: calls.append(a))

    hooks.session_init()

    assert calls == []


def test_session_init_releases_lock_if_spawn_fails(_repo, monkeypatch):
    from witan_code.context import _lock_path

    def _boom(*a, **k):
        raise OSError("cannot spawn")

    monkeypatch.setattr(hooks, "popen_detached", _boom)

    hooks.session_init()

    assert not _lock_path(_repo).exists()


# ── index_and_unlock ──────────────────────────────────────────────────────────


def test_index_and_unlock_releases_lock_on_success(tmp_path, monkeypatch):
    lock = tmp_path / "some.lock"
    lock.mkdir()
    calls = []
    monkeypatch.setattr(
        hooks.indexer, "index_path", lambda target, force: calls.append(target)
    )

    hooks.index_and_unlock(tmp_path, lock)

    assert calls == [tmp_path]
    assert not lock.exists()


def test_index_and_unlock_releases_lock_even_on_failure(tmp_path, monkeypatch):
    lock = tmp_path / "some.lock"
    lock.mkdir()

    def _boom(target, force):
        raise RuntimeError("bad repo")

    monkeypatch.setattr(hooks.indexer, "index_path", _boom)

    hooks.index_and_unlock(tmp_path, lock)  # must not raise

    assert not lock.exists()


# ── reindex_hook ──────────────────────────────────────────────────────────────


def test_reindex_hook_noop_on_empty_payload(monkeypatch):
    calls = []
    monkeypatch.setattr(hooks.indexer, "index_path", lambda *a, **k: calls.append(a))
    hooks.reindex_hook("")
    assert calls == []


def test_reindex_hook_noop_on_malformed_json(monkeypatch):
    calls = []
    monkeypatch.setattr(hooks.indexer, "index_path", lambda *a, **k: calls.append(a))
    hooks.reindex_hook("not json")
    assert calls == []


def test_reindex_hook_noop_without_tool_input(monkeypatch):
    calls = []
    monkeypatch.setattr(hooks.indexer, "index_path", lambda *a, **k: calls.append(a))
    hooks.reindex_hook('{"tool_name": "Edit"}')
    assert calls == []


def test_reindex_hook_noop_when_file_missing(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(hooks.indexer, "index_path", lambda *a, **k: calls.append(a))
    missing = tmp_path / "does-not-exist.py"
    hooks.reindex_hook(f'{{"tool_input": {{"file_path": "{missing}"}}}}')
    assert calls == []


def test_reindex_hook_indexes_the_edited_file(tmp_path, monkeypatch):
    target = tmp_path / "a.py"
    target.write_text("def f(): pass")
    calls = []
    monkeypatch.setattr(
        hooks.indexer, "index_path", lambda p, force: calls.append((p, force))
    )

    hooks.reindex_hook(f'{{"tool_input": {{"file_path": "{target}"}}}}')

    assert calls == [(target, False)]


def test_reindex_hook_resolves_relative_path_against_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "b.py"
    target.write_text("def g(): pass")
    calls = []
    monkeypatch.setattr(hooks.indexer, "index_path", lambda p, force: calls.append(p))

    hooks.reindex_hook('{"tool_input": {"path": "b.py"}}')

    assert calls == [target]


def test_reindex_hook_swallows_index_failure(tmp_path, monkeypatch):
    target = tmp_path / "c.py"
    target.write_text("def h(): pass")

    def _boom(p, force):
        raise RuntimeError("parse error")

    monkeypatch.setattr(hooks.indexer, "index_path", _boom)

    hooks.reindex_hook(
        f'{{"tool_input": {{"filename": "{target}"}}}}'
    )  # must not raise


# ── queued reindex + drainer ──────────────────────────────────────────────────


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _hold(lock: Path, pid: int) -> None:
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(pid))


def test_reindex_hook_in_a_repo_queues_and_spawns_a_drainer(_repo, monkeypatch):
    target = _repo / "a.py"
    target.write_text("def f(): pass")
    indexed, spawned = [], []
    monkeypatch.setattr(hooks.indexer, "index_path", lambda *a, **k: indexed.append(a))
    monkeypatch.setattr(
        hooks, "popen_detached", lambda argv, **kw: spawned.append(argv)
    )

    hooks.reindex_hook(f'{{"tool_input": {{"file_path": "{target}"}}}}')

    root = hooks.repo_module.root(_repo)
    assert indexed == []  # the drainer writes, not the hook
    assert [argv[1:] for argv in spawned] == [
        ["-m", "witan_code", "_drain-pending", str(root)]
    ]
    assert hooks._take_pending(root) == [target]


def test_reindex_hook_leaves_the_path_to_a_live_lock_holder(_repo, monkeypatch):
    target = _repo / "a.py"
    target.write_text("def f(): pass")
    root = hooks.repo_module.root(_repo)
    _hold(hooks._lock_path(root), os.getpid())
    spawned = []
    monkeypatch.setattr(
        hooks, "popen_detached", lambda argv, **kw: spawned.append(argv)
    )

    hooks.reindex_hook(f'{{"tool_input": {{"file_path": "{target}"}}}}')

    assert spawned == []
    assert hooks._take_pending(root) == [target]


def test_drain_pending_indexes_each_queued_file_once_and_unlocks(_repo, monkeypatch):
    root = hooks.repo_module.root(_repo)
    a, b = root / "a.py", root / "b.py"
    for path in (a, b):
        path.write_text("x = 1")
    for path in (a, b, a, a):
        hooks._enqueue(root, path)
    indexed = []
    monkeypatch.setattr(hooks.indexer, "index_path", lambda p, force: indexed.append(p))

    hooks.drain_pending(root)

    assert indexed == [a, b]
    assert not hooks._lock_path(root).exists()
    assert not hooks._has_pending(root)


def test_drain_pending_picks_up_a_path_queued_while_it_held_the_lock(
    _repo, monkeypatch
):
    root = hooks.repo_module.root(_repo)
    a, b = root / "a.py", root / "b.py"
    for path in (a, b):
        path.write_text("x = 1")
    hooks._enqueue(root, a)
    indexed = []

    def _index(p, force):
        indexed.append(p)
        if p == a:
            hooks._enqueue(root, b)  # an edit landing mid-drain

    monkeypatch.setattr(hooks.indexer, "index_path", _index)

    hooks.drain_pending(root)

    assert indexed == [a, b]


def test_drain_pending_skips_a_file_deleted_since_it_was_queued(_repo, monkeypatch):
    root = hooks.repo_module.root(_repo)
    hooks._enqueue(root, root / "gone.py")
    indexed = []
    monkeypatch.setattr(hooks.indexer, "index_path", lambda p, force: indexed.append(p))

    hooks.drain_pending(root)

    assert indexed == []
    assert not hooks._has_pending(root)


def test_drain_pending_continues_past_a_failing_file(_repo, monkeypatch):
    root = hooks.repo_module.root(_repo)
    a, b = root / "a.py", root / "b.py"
    for path in (a, b):
        path.write_text("x = 1")
        hooks._enqueue(root, path)
    indexed = []

    def _index(p, force):
        if p == a:
            raise RuntimeError("write authority changed during preparation")
        indexed.append(p)

    monkeypatch.setattr(hooks.indexer, "index_path", _index)

    hooks.drain_pending(root)

    assert indexed == [b]
    assert not hooks._lock_path(root).exists()


def test_drain_pending_defers_to_a_live_lock_holder(_repo, monkeypatch):
    root = hooks.repo_module.root(_repo)
    a = root / "a.py"
    a.write_text("x = 1")
    hooks._enqueue(root, a)
    _hold(hooks._lock_path(root), os.getpid())
    indexed = []
    monkeypatch.setattr(hooks.indexer, "index_path", lambda p, force: indexed.append(p))

    hooks.drain_pending(root)

    assert indexed == []
    assert hooks._take_pending(root) == [a]


def test_drain_pending_clears_a_lock_whose_holder_died(_repo, monkeypatch):
    root = hooks.repo_module.root(_repo)
    a = root / "a.py"
    a.write_text("x = 1")
    hooks._enqueue(root, a)
    _hold(hooks._lock_path(root), _dead_pid())
    indexed = []
    monkeypatch.setattr(hooks.indexer, "index_path", lambda p, force: indexed.append(p))

    hooks.drain_pending(root)

    assert indexed == [a]


def test_an_unowned_lock_is_trusted_only_for_the_grace_period(tmp_path):
    lock = tmp_path / "some.lock"
    lock.mkdir()
    assert hooks._lock_held(lock)

    old = lock.stat().st_mtime - hooks._UNOWNED_LOCK_GRACE_SECONDS - 1
    os.utime(lock, (old, old))
    assert not hooks._lock_held(lock)


def test_index_and_unlock_applies_edits_queued_during_the_full_index(
    _repo, monkeypatch
):
    root = hooks.repo_module.root(_repo)
    a = root / "a.py"
    a.write_text("x = 1")
    lock = hooks._lock_path(root)
    lock.mkdir(parents=True)
    hooks._enqueue(root, a)
    indexed = []
    monkeypatch.setattr(hooks.indexer, "index_path", lambda p, force: indexed.append(p))

    hooks.index_and_unlock(root, lock)

    assert indexed == [root, a]
    assert not lock.exists()
