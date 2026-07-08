"""Tests for the UserPromptSubmit context-injection hook (witan/context.py),
focused on the CodeBranch "In-Flight Branch" section."""

import asyncio
import subprocess

from .conftest import SCHEMA, requires_omnigraph


def _git(base, *args):
    subprocess.run(
        ["git", "-C", str(base), *args], check=True, capture_output=True, text=True
    )


def _git_repo(path):
    path.mkdir(exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(
        path,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "--allow-empty",
        "-q",
        "-m",
        "init",
    )
    return path


def _unwrap(tool):
    return getattr(tool, "fn", tool)


def _setup(tmp_path, monkeypatch, repo):
    from witan import config as cfg_mod
    from witan import server as srv
    from witan.graph import OmnigraphClient

    store = tmp_path / "graph.omni"
    subprocess.run(
        ["omnigraph", "init", "--schema", str(SCHEMA), str(store)],
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.setenv("WITAN_REPO", repo)
    monkeypatch.setenv("WITAN_AUTHOR", "pytest")
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

    client = OmnigraphClient(str(store), cfg_mod.load().queries_dir)
    monkeypatch.setattr(srv, "client", client)
    return store, cfg_mod.load().queries_dir


@requires_omnigraph
def test_inject_context_surfaces_in_flight_branch_task(tmp_path, monkeypatch):
    from witan import context as ctx_module
    from witan import server as srv

    repo = "https://github.com/test/ctx-repo"
    store, queries_dir = _setup(tmp_path, monkeypatch, repo)

    base = _git_repo(tmp_path / "r")
    _git(base, "checkout", "-q", "-b", "feature/ctx")
    monkeypatch.chdir(base)

    task = _unwrap(srv.task_create)(title="ctx task", description="x")
    asyncio.run(_unwrap(srv.task_claim)(task["slug"], ctx=None))

    text = ctx_module.inject_context(str(store), queries_dir, None)
    assert "## In-Flight Branch" in text
    assert task["slug"] in text
    assert "ctx task" in text
    assert "continue" in text.lower()


@requires_omnigraph
def test_inject_context_omits_section_without_in_flight_branch(tmp_path, monkeypatch):
    """A repo with ready tasks but no CodeBranch for the current checkout
    shows the existing sections, no In-Flight Branch section."""
    from witan import context as ctx_module
    from witan import server as srv

    repo = "https://github.com/test/ctx-repo-2"
    store, queries_dir = _setup(tmp_path, monkeypatch, repo)

    base = _git_repo(tmp_path / "r")
    monkeypatch.chdir(base)  # stays on "main" — no task_claim ever run here

    _unwrap(srv.task_create)(title="untouched task", description="x")

    text = ctx_module.inject_context(str(store), queries_dir, None)
    assert "## In-Flight Branch" not in text
    assert "## Ready Tasks" in text


@requires_omnigraph
def test_inject_context_survives_missing_code_branch_schema(tmp_path, monkeypatch):
    """A store that predates CodeBranch (schema not yet migrated via
    `witan migrate schema`) must not blank the whole context — only the
    branch lookup should degrade, since the two reads are in isolated
    try/except blocks in inject_context."""
    from witan import config as cfg_mod
    from witan import context as ctx_module
    from witan import server as srv
    from witan.graph import OmnigraphClient

    # Simulate a pre-CodeBranch store: apply everything up to (not including)
    # the "Code Branches" section of the real bundled schema.
    real_schema = SCHEMA.read_text()
    legacy_schema = real_schema.split("// ── Code Branches")[0]
    assert legacy_schema != real_schema, (
        "test fixture assumption: schema.pg has the section"
    )
    legacy_schema_file = tmp_path / "legacy-schema.pg"
    legacy_schema_file.write_text(legacy_schema)

    store = tmp_path / "graph.omni"
    subprocess.run(
        ["omnigraph", "init", "--schema", str(legacy_schema_file), str(store)],
        check=True,
        capture_output=True,
        text=True,
    )

    repo = "https://github.com/test/ctx-repo-3"
    monkeypatch.setenv("WITAN_REPO", repo)
    monkeypatch.setenv("WITAN_AUTHOR", "pytest")
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    queries_dir = cfg_mod.load().queries_dir
    client = OmnigraphClient(str(store), queries_dir)
    monkeypatch.setattr(srv, "client", client)

    base = _git_repo(tmp_path / "r")
    _git(base, "checkout", "-q", "-b", "feature/x")
    monkeypatch.chdir(base)

    # Left open (not claimed) so it still shows under Ready Tasks below —
    # the point of this test is the CodeBranch *read* (which inject_context
    # always attempts once repo+branch are known, regardless of whether
    # anything ever claimed a task on this branch) degrading gracefully,
    # not task_claim's own already-covered best-effort behavior.
    _unwrap(srv.task_create)(title="still visible", description="x")

    text = ctx_module.inject_context(str(store), queries_dir, None)
    assert "## Ready Tasks" in text
    assert "still visible" in text
    assert "## In-Flight Branch" not in text


# ── B1: latest session handoff summary on resume ─────────────────────────────


def test_project_session_lines_summary():
    from witan import context as ctx

    proj = {"slug": "wp-x", "phase": "spec"}
    # sessions arrive ordered by started_at asc → the LAST row is newest; summary
    # is truncated to its first line.
    rows = [
        {"summary": "old work", "ended_at": "t1", "phase": "spec"},
        {
            "summary": "newest work\nsecond line ignored",
            "ended_at": "t2",
            "phase": "spec",
        },
    ]
    lines = ctx._project_session_lines(rows, proj)
    assert lines[0] == "  Last session (ended): newest work"
    # an open session (no ended_at) is flagged as such
    lines_open = ctx._project_session_lines(
        [{"summary": "wip", "ended_at": None, "phase": "spec"}], proj
    )
    assert lines_open[0] == "  Last session (still open): wip"
    # no sessions / empty summary → no summary line
    assert ctx._project_session_lines([], proj) == []
    assert (
        ctx._project_session_lines(
            [{"summary": "", "ended_at": "t", "phase": "spec"}], proj
        )
        == []
    )


def test_project_session_lines_staleness_nudge():
    from witan import context as ctx

    proj = {"slug": "wp-x", "phase": "implementation"}
    n = ctx._STALE_SESSION_THRESHOLD
    stale = [
        {"summary": "", "ended_at": "t", "phase": "implementation"} for _ in range(n)
    ]
    lines = ctx._project_session_lines(stale, proj)
    assert any("sessions in `implementation`" in ln for ln in lines)

    # one below threshold → no nudge
    fresh = [
        {"summary": "", "ended_at": "t", "phase": "implementation"}
        for _ in range(n - 1)
    ]
    assert all(
        "sessions in" not in ln for ln in ctx._project_session_lines(fresh, proj)
    )

    # sessions in a DIFFERENT phase than the project's don't count toward staleness
    other = [{"summary": "", "ended_at": "t", "phase": "spec"} for _ in range(n + 2)]
    assert all(
        "sessions in" not in ln for ln in ctx._project_session_lines(other, proj)
    )


@requires_omnigraph
def test_inject_context_surfaces_last_session_summary(tmp_path, monkeypatch):
    import uuid

    from witan import context as ctx_module
    from witan import server as srv

    repo = "https://github.com/test/ctx-repo-4"
    store, queries_dir = _setup(tmp_path, monkeypatch, repo)

    base = _git_repo(tmp_path / "r")
    monkeypatch.chdir(base)

    proj = _unwrap(srv.workflow_project_create)(
        title="ctx proj", description="d", repos=[repo]
    )
    sess = _unwrap(srv.workflow_session_start)(
        project_slug=proj["slug"], session_id=uuid.uuid4().hex, phase="implementation"
    )
    _unwrap(srv.workflow_session_end)(
        sess["session_slug"], summary="left the helper half-wired"
    )

    text = ctx_module.inject_context(str(store), queries_dir, None)
    assert "## Active Workflow Projects" in text
    assert "Last session (ended): left the helper half-wired" in text


# ── A2: honest truncation counts ─────────────────────────────────────────────


@requires_omnigraph
def test_inject_context_truncation_counts_are_honest(tmp_path, monkeypatch):
    from witan import context as ctx_module
    from witan import server as srv

    repo = "https://github.com/test/ctx-trunc"
    store, queries_dir = _setup(tmp_path, monkeypatch, repo)

    base = _git_repo(tmp_path / "r")
    monkeypatch.chdir(base)

    for i in range(6):
        _unwrap(srv.task_create)(title=f"task {i}", description="x")

    text = ctx_module.inject_context(str(store), queries_dir, None)
    # header reports the true total AND flags that only the top 5 are shown
    assert "6 task(s) are ready" in text
    assert "showing the top 5" in text
    # exactly 5 task bullets rendered
    assert text.count("(slug: `tk-") == 5


# ── PR #85 hardening: hook must never raise ──────────────────────────────────


def test_detect_repo_survives_missing_git(monkeypatch):
    from witan import context as ctx

    monkeypatch.delenv("WITAN_REPO", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

    def _boom(*a, **k):
        raise FileNotFoundError("git not installed")

    monkeypatch.setattr(ctx.subprocess, "check_output", _boom)
    # FileNotFoundError (OSError) must degrade to None, not propagate.
    assert ctx._detect_repo() is None


def test_cwd_or_dot_falls_back_on_oserror(monkeypatch):
    from pathlib import Path

    from witan import context as ctx

    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

    def _boom():
        raise OSError("cwd deleted")

    monkeypatch.setattr(Path, "cwd", staticmethod(_boom))
    assert ctx._cwd_or_dot() == "."


def test_cached_repo_and_branch_disabled_skips_branch(monkeypatch):
    from witan import context as ctx

    # WITAN_REPO="" disables detection → repo None → branch detection skipped.
    monkeypatch.setenv("WITAN_REPO", "")

    def _fail_branch():
        raise AssertionError("branch detection should be skipped when no repo")

    monkeypatch.setattr(ctx, "_current_branch", _fail_branch)
    assert ctx._cached_repo_and_branch() == (None, None)


def test_project_session_lines_no_phase_no_crash():
    from witan import context as ctx

    proj = {"slug": "wp-x"}  # no "phase" key
    n = ctx._STALE_SESSION_THRESHOLD
    rows = [{"summary": "s", "ended_at": "t"} for _ in range(n + 1)]
    lines = ctx._project_session_lines(rows, proj)
    # summary line present, but no staleness nudge (and no "None" in output)
    assert any(ln.startswith("  Last session") for ln in lines)
    assert all("sessions in" not in ln for ln in lines)


# ── inject-context output cache (hotfix for slow prompt-path reads) ───────────


@requires_omnigraph
def test_inject_context_output_is_cached(tmp_path, monkeypatch):
    from witan import context as ctx_module
    from witan import server as srv

    repo = "https://github.com/test/ctx-cache"
    store, queries_dir = _setup(tmp_path, monkeypatch, repo)
    # Isolate the on-disk cache to this test.
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)

    base = _git_repo(tmp_path / "r")
    monkeypatch.chdir(base)

    _unwrap(srv.task_create)(title="first task", description="x")
    first = ctx_module.inject_context(str(store), queries_dir, None)
    assert "first task" in first

    # A new task created after the first render must NOT appear until the cache
    # expires — proves the second call served from cache without hitting the graph.
    _unwrap(srv.task_create)(title="second task", description="x")
    cached = ctx_module.inject_context(str(store), queries_dir, None)
    assert cached == first
    assert "second task" not in cached


@requires_omnigraph
def test_inject_context_cache_disabled_by_zero_ttl(tmp_path, monkeypatch):
    from witan import context as ctx_module
    from witan import server as srv

    repo = "https://github.com/test/ctx-cache-off"
    store, queries_dir = _setup(tmp_path, monkeypatch, repo)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("WITAN_CONTEXT_TTL", "0")
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)

    base = _git_repo(tmp_path / "r")
    monkeypatch.chdir(base)

    _unwrap(srv.task_create)(title="alpha", description="x")
    ctx_module.inject_context(str(store), queries_dir, None)
    _unwrap(srv.task_create)(title="bravo", description="x")
    # TTL=0 disables the cache → fresh render includes the new task.
    fresh = ctx_module.inject_context(str(store), queries_dir, None)
    assert "bravo" in fresh


@requires_omnigraph
def test_output_cache_file_is_private_and_atomic(tmp_path, monkeypatch):
    import stat

    from witan import context as ctx_module
    from witan import server as srv

    repo = "https://github.com/test/ctx-priv"
    store, queries_dir = _setup(tmp_path, monkeypatch, repo)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)

    base = _git_repo(tmp_path / "r")
    monkeypatch.chdir(base)
    _unwrap(srv.task_create)(title="private task", description="x")
    ctx_module.inject_context(str(store), queries_dir, None)

    cache_files = list(tmp_path.glob("witan-ctx-*.json"))
    assert cache_files, "cache file should have been written"
    assert stat.S_IMODE(cache_files[0].stat().st_mode) == 0o600
    # atomic replace leaves no process-unique temp file behind
    assert not list(tmp_path.glob("witan-ctx-*.tmp"))


@requires_omnigraph
def test_inject_context_survives_failing_sessions_read(tmp_path, monkeypatch):
    """The batched list_all_sessions read is isolated: if it fails, the resume/
    staleness lines drop but the projects + ready-tasks context still renders."""
    from witan import context as ctx_module
    from witan import server as srv
    from witan.graph import OmnigraphClient

    repo = "https://github.com/test/ctx-sessfail"
    store, queries_dir = _setup(tmp_path, monkeypatch, repo)
    base = _git_repo(tmp_path / "r")
    monkeypatch.chdir(base)

    _unwrap(srv.workflow_project_create)(title="proj", description="d", repos=[repo])
    _unwrap(srv.task_create)(title="visible task", description="x")

    orig_read = OmnigraphClient.read

    def _read(self, query_file, query_name, params):
        if query_name == "list_all_sessions":
            raise RuntimeError("sessions query boom")
        return orig_read(self, query_file, query_name, params)

    monkeypatch.setattr(OmnigraphClient, "read", _read)

    text = ctx_module.inject_context(str(store), queries_dir, None)
    assert "## Active Workflow Projects" in text
    assert "## Ready Tasks" in text
    assert "visible task" in text
