"""Unit tests for the SessionStart/PostToolUse hook logic in hooks.py.

``indexer.index_path`` and process spawning are monkeypatched throughout:
these tests exercise hooks.py's own orchestration (git-repo check, lock
acquire/release, stdin-JSON parsing, path resolution), not the real indexer
or a real detached child.
"""

import subprocess

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
    monkeypatch.setattr(
        hooks, "popen_detached", lambda argv, **kw: calls.append((argv, kw))
    )

    hooks.session_init()

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[1:4] == ["-m", "witan_code", "_index-and-unlock"]
    assert argv[4] == str(_repo)
    lock_arg = argv[5]
    from pathlib import Path

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
