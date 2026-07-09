"""Unit tests for the `witan-code inject-context` UserPromptSubmit hook backend.

No omnigraph binary required: store-stats lookup is monkeypatched since these
tests exercise context.py's own orchestration (repo detection, lock-file
check, store-exists branching), not the underlying query.
"""

from pathlib import Path

from witan_code import context


def _lock(tmp_path, monkeypatch, project_dir):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
    sanitized = str(project_dir).replace("/", "_")
    return tmp_path / f"codegraph-init-{sanitized}.lock"


def test_inject_context_empty_without_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("WITAN_REPO", raising=False)
    monkeypatch.chdir(tmp_path)  # no .git anywhere above a tmp dir
    assert context.inject_context() == ""


def test_inject_context_empty_when_no_store_and_no_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/cg")
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    _lock(tmp_path, monkeypatch, tmp_path / "project")

    assert context.inject_context() == ""


def test_inject_context_reports_in_progress_index(tmp_path, monkeypatch):
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/cg")
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    lock = _lock(tmp_path, monkeypatch, tmp_path / "project")
    lock.mkdir(parents=True)

    text = context.inject_context()

    assert "test/cg" in text
    assert "Indexing" in text
    assert "background" in text


def test_inject_context_reports_indexed_store(tmp_path, monkeypatch):
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/cg")
    code_dir = tmp_path / "code"
    monkeypatch.setenv("WITAN_CODE_DIR", str(code_dir))
    _lock(tmp_path, monkeypatch, tmp_path / "project")

    store = code_dir / "https_github.com_test_cg.omni"
    store.mkdir(parents=True)
    (store / "data.lance").write_text("x")
    monkeypatch.setattr(
        context, "_code_store_stats", lambda s: ("https://github.com/test/cg", "3")
    )

    text = context.inject_context()

    assert "https://github.com/test/cg" in text
    assert "3 files" in text
    assert "code_search_symbol" in text
    assert "background reindex is currently running" not in text


def test_inject_context_notes_in_progress_alongside_existing_store(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/cg")
    code_dir = tmp_path / "code"
    monkeypatch.setenv("WITAN_CODE_DIR", str(code_dir))
    lock = _lock(tmp_path, monkeypatch, tmp_path / "project")
    lock.mkdir(parents=True)

    store = code_dir / "https_github.com_test_cg.omni"
    store.mkdir(parents=True)
    (store / "data.lance").write_text("x")
    monkeypatch.setattr(
        context, "_code_store_stats", lambda s: ("https://github.com/test/cg", "3")
    )

    text = context.inject_context()

    assert "background reindex is currently running" in text


def test_indexing_in_progress_matches_sanitized_project_dir(tmp_path, monkeypatch):
    project_dir = tmp_path / "some" / "project"
    lock = _lock(tmp_path, monkeypatch, project_dir)

    assert context.indexing_in_progress() is False

    lock.mkdir(parents=True)
    assert context.indexing_in_progress() is True


def test_project_dir_falls_back_to_root_when_cwd_deleted(monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

    def _boom():
        raise OSError("cwd was deleted out from under this process")

    monkeypatch.setattr(context.os, "getcwd", _boom)

    assert context._project_dir() == Path("/")


def test_inject_context_degrades_when_dir_stats_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/cg")
    code_dir = tmp_path / "code"
    monkeypatch.setenv("WITAN_CODE_DIR", str(code_dir))
    _lock(tmp_path, monkeypatch, tmp_path / "project")

    store = code_dir / "https_github.com_test_cg.omni"
    store.mkdir(parents=True)
    monkeypatch.setattr(
        context, "_code_store_stats", lambda s: ("https://github.com/test/cg", "3")
    )
    monkeypatch.setattr(
        context,
        "_dir_stats",
        lambda s: (_ for _ in ()).throw(OSError("vanished mid-walk")),
    )

    text = context.inject_context()

    assert "3 files." in text  # no "last updated" clause, but not blanked either
    assert "code_search_symbol" in text
