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
    return context._lock_path(project_dir)


def _stub_store_stats(monkeypatch, health=None):
    """Answer the store lookups without an omnigraph binary or a real store.

    ``health`` overrides what the readiness probe reports; the default is a
    healthy 3-file store. The bridge probe is stubbed to "no bridge here"
    unless a caller says otherwise, so the coverage line stays about repo
    counts in the tests that are about repo counts.
    """
    monkeypatch.setattr(
        context.store_module,
        "repo_for_store",
        lambda s, cfg=None: "https://github.com/test/cg",
    )
    monkeypatch.setattr(
        context.store_module,
        "store_health",
        lambda s, cfg=None: health or context.store_module.StoreHealth(s, 3),
    )
    monkeypatch.setattr(context, "_bridge_ok", lambda cfg: None)


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
    _stub_store_stats(monkeypatch)

    text = context.inject_context()

    assert "https://github.com/test/cg" in text
    assert "3 files" in text
    assert "code_find_definition" in text
    assert "background reindex is currently running" not in text


def test_inject_context_names_the_toolsearch_unlock(tmp_path, monkeypatch):
    """The block must name the call that makes `code_*` callable.

    The tools reach the agent deferred (name only, no schema) in every measured
    session, so a bare "prefer code_* over grep" points at tools that are not in
    the tool list. The `+code_` query form is the one that works: `select:` needs
    the full `mcp__<server>__` prefix, which depends on the user's MCP config.
    """
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/cg")
    code_dir = tmp_path / "code"
    monkeypatch.setenv("WITAN_CODE_DIR", str(code_dir))
    _lock(tmp_path, monkeypatch, tmp_path / "project")

    store = code_dir / "https_github.com_test_cg.omni"
    store.mkdir(parents=True)
    _stub_store_stats(monkeypatch)

    text = context.inject_context()

    assert "ToolSearch" in text
    assert '"+code_ find_definition callers impact"' in text
    assert "select:" not in text  # would need a prefix the hook cannot know
    assert "/witan-code" in text  # the skill, per the task's direction 3


def test_inject_context_warns_when_no_other_repo_is_indexed(tmp_path, monkeypatch):
    """Zero cross-repo coverage is the one thing an agent gets silently wrong."""
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/cg")
    code_dir = tmp_path / "code"
    monkeypatch.setenv("WITAN_CODE_DIR", str(code_dir))
    _lock(tmp_path, monkeypatch, tmp_path / "project")

    store = code_dir / "https_github.com_test_cg.omni"
    store.mkdir(parents=True)
    _stub_store_stats(monkeypatch)

    text = context.inject_context()

    assert "No other repo is indexed" in text
    assert "absence of data, not absence of consumers" in text


def test_inject_context_reports_cross_repo_coverage(tmp_path, monkeypatch):
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/cg")
    code_dir = tmp_path / "code"
    monkeypatch.setenv("WITAN_CODE_DIR", str(code_dir))
    _lock(tmp_path, monkeypatch, tmp_path / "project")

    store = code_dir / "https_github.com_test_cg.omni"
    store.mkdir(parents=True)
    for other in (
        "https_github.com_test_other.omni",
        "https_github.com_test_third.omni",
    ):
        (code_dir / other).mkdir()
    _stub_store_stats(monkeypatch)

    text = context.inject_context()

    assert "2 other repos indexed" in text  # the current repo is not counted
    assert "No other repo is indexed" not in text


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
    _stub_store_stats(monkeypatch)

    text = context.inject_context()

    assert "background reindex is currently running" in text


def test_inject_context_says_so_when_the_store_cannot_be_read(tmp_path, monkeypatch):
    """An unreadable store rendered as "? files", which reads as cosmetic.

    It is not: every `code_*` call against the repo errors. The block has to
    push the agent off the tools rather than quietly understate the count.
    """
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/cg")
    code_dir = tmp_path / "code"
    monkeypatch.setenv("WITAN_CODE_DIR", str(code_dir))
    _lock(tmp_path, monkeypatch, tmp_path / "project")

    store = code_dir / "https_github.com_test_cg.omni"
    store.mkdir(parents=True)
    _stub_store_stats(
        monkeypatch,
        health=context.store_module.StoreHealth(
            None, None, error="internal schema v4", stale_schema=True
        ),
    )

    text = context.inject_context()

    assert "CANNOT BE READ" in text
    assert "witan-code doctor" in text
    assert "? files" not in text


def test_inject_context_prefers_in_flight_to_unreadable(tmp_path, monkeypatch):
    """A store being created right now is not a broken one.

    `ensure_store` makes the directory before `omnigraph init` fills it, so a
    prompt landing inside that window sees a store that exists and will not
    open. The in-flight message already says not to trust it.
    """
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/cg")
    code_dir = tmp_path / "code"
    monkeypatch.setenv("WITAN_CODE_DIR", str(code_dir))
    lock = _lock(tmp_path, monkeypatch, tmp_path / "project")
    lock.mkdir(parents=True)

    store = code_dir / "https_github.com_test_cg.omni"
    store.mkdir(parents=True)
    _stub_store_stats(
        monkeypatch,
        health=context.store_module.StoreHealth(None, None, error="half-written"),
    )

    text = context.inject_context()

    assert "CANNOT BE READ" not in text
    assert "background reindex is currently running" in text


def test_coverage_line_refuses_to_claim_cross_repo_works_with_a_dead_bridge(
    tmp_path, monkeypatch
):
    """The line that was false for six weeks.

    Every `code_interface_*` tool reads the bridge graph and nothing else does,
    so a repo count says nothing about whether they resolve. Claiming they do
    while the bridge cannot be opened is worse than silence: it turns a failed
    query into "nothing consumes this".
    """
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/cg")
    code_dir = tmp_path / "code"
    monkeypatch.setenv("WITAN_CODE_DIR", str(code_dir))
    _lock(tmp_path, monkeypatch, tmp_path / "project")

    store = code_dir / "https_github.com_test_cg.omni"
    store.mkdir(parents=True)
    (code_dir / "https_github.com_test_other.omni").mkdir()
    _stub_store_stats(monkeypatch)
    monkeypatch.setattr(context, "_bridge_ok", lambda cfg: False)

    text = context.inject_context()

    assert "cross-repo `code_interface_*` resolve" not in text
    assert "code_interface_*` / `code_cross_repo_impact` call FAILS" in text


def test_lock_path_does_not_collide_on_sanitization(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    a = context._lock_path(Path("/tmp/a/b"))
    b = context._lock_path(Path("/tmp/a_b"))

    assert a != b  # a naive "/" -> "_" replace would collide these


def test_lock_path_name_is_bounded_regardless_of_project_dir_length(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    long_dir = Path("/" + ("deeply-nested-directory-" * 20) + "/checkout")

    lock = context._lock_path(long_dir)

    assert len(lock.name) < 255  # well under common filesystem filename limits


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
    _stub_store_stats(monkeypatch)
    monkeypatch.setattr(
        context.store_module,
        "dir_stats",
        lambda s: (_ for _ in ()).throw(OSError("vanished mid-walk")),
    )

    text = context.inject_context()

    assert "3 files." in text  # no "last updated" clause, but not blanked either
    assert "code_find_definition" in text


def test_inject_context_block_stays_small(tmp_path, monkeypatch):
    """Guard against creep: this is prepended to every prompt, forever.

    Not a style preference — an extra 100 chars here is paid on every prompt of
    every session in every repo that has witan-code installed.
    """
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/cg")
    code_dir = tmp_path / "code"
    monkeypatch.setenv("WITAN_CODE_DIR", str(code_dir))
    _lock(tmp_path, monkeypatch, tmp_path / "project")

    store = code_dir / "https_github.com_test_cg.omni"
    store.mkdir(parents=True)
    _stub_store_stats(monkeypatch)

    assert len(context.inject_context()) < 600
