"""Tests for the UserPromptSubmit context-injection hook (witan/context.py),
focused on the CodeBranch "In-Flight Branch" section, and on the
`witan inject-context` command never failing the hook."""

import asyncio
import logging
import os
import subprocess
import sys
import textwrap
import time

import pytest

from witan_core.observability import configure_logging, reset_logging

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


class _NoElicitCtx:
    """A ctx whose elicit always errors, so async tools fall back to their
    non-interactive default (mirrors conftest._NoElicitCtx)."""

    async def elicit(self, *args, **kwargs):
        raise RuntimeError("elicitation unsupported in tests")


def _unwrap(tool):
    """Return a directly-callable tool. Async tools (those taking ``ctx``) are
    run to completion with a no-elicit ctx injected, so sync call sites keep
    working and get today's non-interactive behavior."""
    import asyncio
    import inspect

    fn = getattr(tool, "fn", tool)
    if inspect.iscoroutinefunction(fn):

        def runner(*args, **kwargs):
            kwargs.setdefault("ctx", _NoElicitCtx())
            return asyncio.run(fn(*args, **kwargs))

        return runner
    return fn


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
    _unwrap(srv.task_claim)(task["slug"])

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


@requires_omnigraph
def test_inject_context_warns_on_stale_repo_case(tmp_path, monkeypatch):
    """A task written under a differently-cased (pre-#142-fix) repo key
    surfaces a nudge to run `witan migrate repo-keys` — the case-insensitive,
    not-identical match against the (now-canonical) detected repo."""
    from witan import context as ctx_module
    from witan import server as srv

    repo = "https://github.com/mitodl/ctx-case-repo"
    stale = "https://github.com/MITODL/ctx-case-repo"
    store, queries_dir = _setup(tmp_path, monkeypatch, repo)

    srv.client.change(
        "mutations.gq",
        "insert_task",
        {
            "slug": "tk-stale-ctx-aaaaaa",
            "title": "stale-cased task",
            "description": "",
            "repo": stale,
            "type": "task",
            "status": "open",
            "priority": "p2",
            "project_slug": None,
            "parent_slug": None,
            "blocked_by": None,
            "assignee": None,
            "external_uri": None,
            "author": "pytest",
            "symbol_refs": None,
            "tags": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "claimed_at": None,
        },
    )

    text = ctx_module.inject_context(str(store), queries_dir, None)
    assert "## ⚠ Unmigrated Repo Keys" in text
    assert "witan migrate repo-keys" in text


@requires_omnigraph
def test_inject_context_no_warning_when_repo_keys_are_canonical(tmp_path, monkeypatch):
    from witan import context as ctx_module
    from witan import server as srv

    repo = "https://github.com/test/ctx-clean-repo"
    store, queries_dir = _setup(tmp_path, monkeypatch, repo)
    _unwrap(srv.task_create)(title="clean task", description="x")

    text = ctx_module.inject_context(str(store), queries_dir, None)
    assert "Unmigrated Repo Keys" not in text


@requires_omnigraph
def test_inject_context_no_warning_for_self_hosted_path_case_difference(
    tmp_path, monkeypatch
):
    """A self-hosted (non-GitHub/GitLab) repo's path case is NOT folded by
    normalise() — it may be a genuinely different, case-sensitive-path repo.
    A task recorded under a path-case-different value for the same host must
    not trigger the migration nudge, since `witan migrate repo-keys` would
    leave it alone (it only folds path case for github.com/gitlab.com)."""
    from witan import context as ctx_module
    from witan import server as srv

    repo = "https://git.example.com/Org/Repo"
    other_case = "https://git.example.com/org/repo"
    store, queries_dir = _setup(tmp_path, monkeypatch, repo)

    srv.client.change(
        "mutations.gq",
        "insert_task",
        {
            "slug": "tk-selfhosted-case-bbbbbb",
            "title": "different repo, coincidental case match",
            "description": "",
            "repo": other_case,
            "type": "task",
            "status": "open",
            "priority": "p2",
            "project_slug": None,
            "parent_slug": None,
            "blocked_by": None,
            "assignee": None,
            "external_uri": None,
            "author": "pytest",
            "symbol_refs": None,
            "tags": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "claimed_at": None,
        },
    )

    text = ctx_module.inject_context(str(store), queries_dir, None)
    assert "Unmigrated Repo Keys" not in text


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


def test_the_ready_task_list_says_to_claim_before_working(tmp_path, monkeypatch):
    """The list is an invitation to start work, so it has to name the first step.

    It used to end with "use task_update/task_close ... to claim and progress
    them", which reads as bookkeeping to do at some point. An unclaimed task
    being actively worked is indistinguishable from an idle one, and two
    sessions took the same task off this list on the same day.
    """
    from witan import context as ctx_module
    from witan import server as srv

    repo = "https://github.com/test/ctx-claim"
    store, queries_dir = _setup(tmp_path, monkeypatch, repo)

    base = _git_repo(tmp_path / "r")
    monkeypatch.chdir(base)

    _unwrap(srv.task_create)(title="something ready", description="x")

    text = ctx_module.inject_context(str(store), queries_dir, None)

    assert "task_claim" in text
    assert text.index("task_claim") < text.index("task_close")


@requires_omnigraph
def test_codex_gets_the_block_for_its_cwd_as_plain_text(tmp_path, monkeypatch):
    """Codex runs the UserPromptSubmit hook in the session's cwd, with no
    ``CLAUDE_PROJECT_DIR`` and no repo override, so the repo comes from that
    checkout's remote. It takes plain stdout as context, but reads stdout that
    starts with ``{`` or ``[`` as JSON and fails the hook when it isn't valid
    hook output (``looks_like_json`` in codex-rs/hooks/src/events/
    user_prompt_submit.rs, rust-v0.162.1), so the block must never start
    with either."""
    from witan import context as ctx_module
    from witan import server as srv

    repo = "https://github.com/test/ctx-codex"
    store, queries_dir = _setup(tmp_path, monkeypatch, repo)
    _unwrap(srv.task_create)(title="codex ready task", description="x")
    monkeypatch.delenv("WITAN_REPO")

    base = _git_repo(tmp_path / "r")
    _git(base, "remote", "add", "origin", f"{repo}.git")
    monkeypatch.chdir(base)

    text = ctx_module.inject_context(str(store), queries_dir, None)

    assert "codex ready task" in text
    assert text.startswith("## ")


# ── PR #85 hardening: hook must never raise ──────────────────────────────────


def test_detect_repo_survives_missing_git(monkeypatch):
    from witan import context as ctx
    from witan import repo as repo_module

    monkeypatch.delenv("WITAN_REPO", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

    def _boom(*a, **k):
        raise FileNotFoundError("git not installed")

    # _detect_repo now shares repo.git_remote_url, which drives subprocess.run;
    # a missing git binary (OSError) must degrade to None, not propagate.
    monkeypatch.setattr(repo_module.subprocess, "run", _boom)
    assert ctx._detect_repo() is None


def test_detect_repo_normalises_witan_repo_env(monkeypatch):
    """WITAN_REPO must canonicalize the same way an auto-detected remote does
    (issue #142) — else the hook's injected repo scope can drift in case from
    what ``repo.detect()`` returns for the same env var."""
    from witan import context as ctx

    monkeypatch.setenv("WITAN_REPO", "https://GitHub.com/MITODL/OL-Django")
    assert ctx._detect_repo() == "https://github.com/mitodl/ol-django"


@pytest.fixture(autouse=True)
def _deterministic_logging():
    """Pin logging to a fresh handler on the *current* stderr for each test.

    Two things make the diagnostics otherwise untestable with capsys once the
    whole suite runs together. structlog caches a module-level ``get_logger``
    proxy on first use, so ``witan.context``'s logger keeps whatever factory was
    active then; and ``configure_logging`` builds a ``StreamHandler`` bound to
    whatever ``sys.stderr`` was at that moment, so a call in an earlier test
    leaves records going to that test's capture. Reconfiguring per test rebinds
    the handler and makes the assertions below about the code rather than about
    suite ordering.

    ``json`` specifically, because that is what a deployed pod runs: it puts the
    exception through ``ExceptionRenderer`` in the pipeline, so a swallowed
    error's cause lands *in the event* where caplog (and Loki) can see it. In
    ``console`` mode the renderer formats the traceback at handler time instead,
    and the cause is invisible to any in-process assertion.

    The genuinely-unconfigured hook path is covered separately, in a subprocess
    -- see ``test_hook_writes_nothing_to_stdout_in_a_fresh_process``.
    """
    configure_logging(log_format="json", level="DEBUG", force=True)
    yield
    reset_logging()
    logging.getLogger().handlers.clear()


@requires_omnigraph
def test_inject_context_debug_reports_to_stderr(tmp_path, monkeypatch, capsys, caplog):
    """--debug prints detection/read diagnostics to stderr, leaving stdout
    (the injected block) untouched."""
    from witan import context as ctx_module

    repo = "https://github.com/test/ctx-debug"
    store, queries_dir = _setup(tmp_path, monkeypatch, repo)
    base = _git_repo(tmp_path / "r")
    monkeypatch.chdir(base)

    text = ctx_module.inject_context(str(store), queries_dir, None, debug=True)
    captured = capsys.readouterr()
    # The diagnostics are structlog events now rather than a
    # "[witan inject-context]" prefix, so they are asserted through caplog;
    # that they reach stderr and never stdout is what the subprocess test at
    # the bottom of this file proves, in a process that mirrors the real hook.
    assert "witan.context.debug" in caplog.text
    assert "detected repo=" in caplog.text
    # The returned block itself must not carry the diagnostics.
    assert "witan.context.debug" not in text
    assert captured.out == ""


def test_inject_context_debug_surfaces_failure_reason(monkeypatch, capsys, caplog):
    """A broken graph read is swallowed (returns "") but --debug prints why."""
    from witan import context as ctx_module

    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/broken")

    def _boom(*a, **k):
        raise RuntimeError("graph is on fire")

    # OmnigraphClient construction/reads happen inside the main try — force a
    # failure and assert the swallow is annotated on stderr.
    monkeypatch.setattr(ctx_module, "OmnigraphClient", _boom)
    monkeypatch.setattr(ctx_module, "_read_output_cache", lambda *a, **k: None)

    out = ctx_module.inject_context("nonexistent", None, None, debug=True)
    captured = capsys.readouterr()
    assert out == ""
    assert "FAILED building context" in caplog.text
    # The cause is rendered into the event by ExceptionRenderer under the json
    # format the fixture pins -- the same shape an operator reads in Loki.
    assert "graph is on fire" in caplog.text
    assert captured.out == ""


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


# ── inject-context never fails the UserPromptSubmit hook ───────────


def test_inject_context_cli_survives_a_broken_config(tmp_path, monkeypatch, capsys):
    """The command documents "always exits 0 and never blocks", but the config
    load was unguarded and ran first — so it failed before the graph-missing
    and debug machinery could help. `load_toml` fails the whole document, so
    one stray character anywhere in config.toml reaches this path."""
    from witan.cli import hooks

    broken = tmp_path / "config.toml"
    broken.write_text("[targets.personal\ngraph = 'x'\n")
    monkeypatch.setenv("WITAN_CONFIG", str(broken))

    hooks.inject_context()  # must not raise

    out = capsys.readouterr()
    assert out.out == ""


def test_inject_context_cli_survives_a_stale_witan_target(
    tmp_path, monkeypatch, capsys
):
    """The second trigger: `load()` also raises for an explicitly-requested
    target that isn't defined, so a stale WITAN_TARGET left in the environment
    breaks the hook even with a perfectly valid config file."""
    from witan.cli import hooks

    valid = tmp_path / "config.toml"
    valid.write_text('[targets.work]\ngraph = "work.omni"\n')
    monkeypatch.setenv("WITAN_CONFIG", str(valid))
    monkeypatch.setenv("WITAN_TARGET", "gone")

    hooks.inject_context()  # must not raise

    out = capsys.readouterr()
    assert out.out == ""


@pytest.mark.parametrize(
    ("config_text", "target"),
    [
        ("[targets.personal\ngraph = 'x'\n", None),
        ('[targets.work]\ngraph = "work.omni"\n', "gone"),
    ],
)
def test_inject_context_cli_debug_explains_on_stderr_only(
    config_text, target, tmp_path, monkeypatch, capsys, caplog
):
    """--debug is how a blank block is diagnosed, so the reason has to reach
    stderr — while stdout stays empty, since anything printed there is
    prepended to the user's prompt."""
    from witan.cli import hooks

    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(config_text)
    monkeypatch.setenv("WITAN_CONFIG", str(cfg_file))
    if target:
        monkeypatch.setenv("WITAN_TARGET", target)

    hooks.inject_context(debug=True)

    out = capsys.readouterr()
    assert out.out == ""
    assert "witan.hook.config_load_failed" in caplog.text


def test_hook_writes_nothing_to_stdout_in_a_fresh_process(tmp_path):
    """The hook must not write to stdout in a process that never configured logging.

    This is the guard for the trap the structlog migration introduced. The hook
    runs as a bare ``witan inject-context`` process that never calls
    ``configure_observability()``, and structlog's own out-of-the-box logger
    factory writes to STDOUT -- so converting these diagnostics from
    ``print(..., file=sys.stderr)`` to log calls would have silently started
    injecting them into the user's prompt, where they would be read as part of
    it. ``witan_core.observability.logging`` pins the unconfigured fallback to
    stderr; this proves the hook actually benefits from that.

    A subprocess, not a fixture, because that is the only way to get a genuinely
    pristine interpreter: an in-process version is at the mercy of whichever
    earlier test last configured logging, since structlog caches a module-level
    logger on first use. It is also exactly how the hook runs in production.
    """
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[targets.work]\ngraph = "work.omni"\n')

    program = textwrap.dedent(
        """
        from witan import context as ctx
        from witan.cli import hooks

        # Every degradation the hook can hit, through the real entry points.
        hooks.inject_context(debug=True)
        ctx._output_cache_ttl()
        ctx._detect_repo()
        ctx._current_branch()
        ctx._read_output_cache("/nonexistent/graph.omni", None, None)
        ctx.inject_context("/nonexistent/graph.omni", ".", None, debug=True)
        """
    )
    # Inherit the real environment rather than building one from scratch: the
    # subprocess still needs whatever makes Python work here (locale, cert
    # paths, uv/venv resolution), and a hand-built dict would fail for reasons
    # that have nothing to do with what is being asserted.
    env = os.environ.copy()
    # But strip witan/OTel configuration the developer's shell may already
    # carry, so the run is driven only by what this test sets — a stray
    # WITAN_TARGET or WITAN_LOG_LEVEL=ERROR would otherwise quietly change what
    # the hook does, or silence it into passing vacuously.
    for key in [
        k for k in env if k.startswith(("WITAN_", "OTEL_")) or k == "LOG_LEVEL"
    ]:
        del env[key]
    env |= {
        "WITAN_CONFIG": str(cfg_file),
        "WITAN_TARGET": "does-not-exist",
        "WITAN_CONTEXT_TTL": "not-a-number",
        "WITAN_REPO": "",
        # DEBUG so the debug-level degradations are emitted rather than
        # filtered out, which would make this pass vacuously.
        "WITAN_LOG_LEVEL": "DEBUG",
    }
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.stdout == "", f"hook wrote to stdout: {result.stdout!r}"
    # And it genuinely reported the degradations rather than being silent.
    assert "witan." in result.stderr


def test_inject_context_cli_survives_an_unreachable_deployment(
    tmp_path, monkeypatch, capsys
):
    """A configured-but-down deployment must not reach an agent's prompt.

    A `remote_url`-only target has no direct graph endpoint the CLI is
    allowed to open, so this command routes through the same tool-calling
    proxy `_srv()` builds for every other command (agent-kit#261's mechanism,
    fixed here for this path too — see
    tk-witan-hook-context-reads-the-local-store-on-a-de-dfb2c9). It must
    degrade to an empty block rather than raise when the deployment cannot be
    reached, exactly like `session-checkpoint` already does.
    """
    from witan.cli import hooks
    from witan_core.remote.proxy import RemoteMCPProxy

    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("")
    monkeypatch.setenv("WITAN_CONFIG", str(cfg_file))
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.setenv("WITAN_REMOTE_URL", "https://witan.invalid/mcp")
    monkeypatch.setenv("WITAN_OIDC_ISSUER", "https://sso.invalid/realms/ol")
    attempted: list[str] = []

    async def _refused(_self, name, _args, _kwargs):
        attempted.append(name)
        raise RuntimeError("witan.invalid: connection refused")

    monkeypatch.setattr(RemoteMCPProxy, "_invoke", _refused)

    hooks.inject_context()  # must not raise

    assert capsys.readouterr().out == ""
    # Not vacuous: the hook got as far as a tool call before degrading. A
    # cached block, or an early return, would leave this empty.
    assert attempted


class _FakeRemoteServer:
    """Records every call it receives and answers with fixed, known data.

    Stands in for the tool-calling proxy ``_srv()``/``remote_proxy()`` build.
    ``inject_context_remote`` awaits ``dispatch(name, **kwargs)`` and
    ``ensure_tool_schema()`` on it, the proxy's async entry points, so this
    routes each dispatched name to a plain method of the same name.
    """

    def __init__(
        self,
        projects,
        ready,
        sessions_by_project,
        branch_tasks=None,
        raises=(),
        held=None,
        comments=None,
        hangs=(),
    ):
        self.calls: list[tuple[str, dict]] = []
        self.primed = 0
        # Tool names this fake accepts and never answers, standing in for a
        # deployment that holds the response stream open.
        self._hangs = set(hangs)
        self._projects = projects
        self._ready = ready
        self._sessions_by_project = sessions_by_project
        self._branch_tasks = branch_tasks or []
        self._held = held or []
        self._comments = comments or {}
        # Tool names this fake answers with an error, standing in for a
        # deployment that predates the tool (its proxy raises on an unknown
        # tool name rather than returning empty).
        self._raises = set(raises)

    def _guard(self, name):
        if name in self._raises:
            raise RuntimeError(f"Unknown tool: {name}")

    async def dispatch(self, name, /, **kwargs):
        result = getattr(self, name)(**kwargs)
        if name in self._hangs:
            await asyncio.sleep(3600)
        return result

    async def ensure_tool_schema(self):
        self.primed += 1

    def workflow_project_list(self, **kwargs):
        self.calls.append(("workflow_project_list", kwargs))
        self._guard("workflow_project_list")
        return self._projects

    def task_ready(self, **kwargs):
        self.calls.append(("task_ready", kwargs))
        self._guard("task_ready")
        return self._ready

    def workflow_session_list(self, **kwargs):
        self.calls.append(("workflow_session_list", kwargs))
        self._guard("workflow_session_list")
        return self._sessions_by_project.get(kwargs.get("project_slug"), [])

    def task_for_branch(self, **kwargs):
        self.calls.append(("task_for_branch", kwargs))
        self._guard("task_for_branch")
        return self._branch_tasks

    def task_list(self, **kwargs):
        self.calls.append(("task_list", kwargs))
        self._guard("task_list")
        return self._held

    def task_get(self, **kwargs):
        self.calls.append(("task_get", kwargs))
        self._guard("task_get")
        return {"comments": self._comments.get(kwargs.get("slug"), [])}


def test_inject_context_remote_reads_through_the_proxy(tmp_path, monkeypatch):
    """The successful remote path, not just its failure mode.

    ``test_inject_context_cli_survives_an_unreachable_deployment`` above only
    proves a dead proxy degrades to empty output — it never exercises a
    proxy that actually answers, so a regression in the tool names/arguments,
    session aggregation, or rendering could ship with that test still green
    (Copilot review on agent-kit#272). This pins the exact calls made and the
    resulting text.
    """
    from witan import context as ctx_module

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("WITAN_CONTEXT_TTL", "0")
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)

    repo = "https://github.com/test/ctx-remote"
    monkeypatch.setenv("WITAN_REPO", repo)

    server = _FakeRemoteServer(
        projects=[
            {"slug": "wp-remote", "title": "Remote Project", "phase": "implementation"}
        ],
        ready=[
            {
                "slug": "tk-remote",
                "title": "Remote Task",
                "priority": "p1",
                "status": "open",
            }
        ],
        sessions_by_project={},
    )

    text = ctx_module.inject_context_remote(server, "https://witan.example.org/mcp")

    assert ("workflow_project_list", {"repo": repo, "status": "active"}) in server.calls
    assert ("task_ready", {"repo": repo, "limit": 10000}) in server.calls
    assert (
        "workflow_session_list",
        {"project_slug": "wp-remote"},
    ) in server.calls

    assert "## Active Workflow Projects" in text
    assert "Remote Project" in text
    assert "wp-remote" in text
    assert "## Ready Tasks" in text
    assert "Remote Task" in text
    # This proxy reports no branch tasks, so the block must be absent — the
    # rendered-block case is pinned separately below.
    assert "## In-Flight Branch" not in text
    # Stale-repo-case detection still has no remote-tool equivalent, so it must
    # be absent rather than incidentally empty (inject_context_remote's
    # docstring).
    assert "Unmigrated Repo Keys" not in text


def test_inject_context_remote_renders_the_in_flight_branch_block(
    tmp_path, monkeypatch
):
    """The anti-duplicate-work signal, on the path every deployed user is on.

    Until tk-the-in-flight-branch-anti-duplicate-work-signal--073f96 this block
    was hard-coded empty for a remote target, so it fired only in the local
    single-user setup where it mattered least. Closed tasks are filtered out
    here, matching the local path — a branch whose task is done is not in
    flight.
    """
    from witan import context as ctx_module

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("WITAN_CONTEXT_TTL", "0")
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)

    repo = "https://github.com/test/ctx-remote-branch"
    monkeypatch.setenv("WITAN_REPO", repo)
    monkeypatch.setattr(ctx_module, "_current_branch", lambda: "feature/in-flight")

    server = _FakeRemoteServer(
        projects=[],
        ready=[],
        sessions_by_project={},
        branch_tasks=[
            {
                "slug": "tk-held",
                "title": "Held Elsewhere",
                "status": "in_progress",
                "assignee": "someone-else@example.org",
            },
            {"slug": "tk-done", "title": "Already Finished", "status": "closed"},
        ],
    )

    text = ctx_module.inject_context_remote(server, "https://witan.example.org/mcp")

    assert (
        "task_for_branch",
        {"branch": "feature/in-flight", "repo": repo},
    ) in server.calls
    assert "## In-Flight Branch" in text
    assert "Held Elsewhere" in text
    # The holder is the whole point on a shared graph: it turns "this branch is
    # taken" into "go talk to this person".
    assert "someone-else@example.org" in text
    assert "Already Finished" not in text


def test_inject_context_remote_branch_read_failure_keeps_the_rest(
    tmp_path, monkeypatch
):
    """A deployment that predates ``task_for_branch`` answers with an unknown-
    tool error. That must cost the branch block only — losing the projects and
    ready-tasks block over it would make the hook worse than before it existed,
    which is exactly the isolation the local path already has."""
    from witan import context as ctx_module

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("WITAN_CONTEXT_TTL", "0")
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)

    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/ctx-remote-oldsrv")
    monkeypatch.setattr(ctx_module, "_current_branch", lambda: "feature/old-server")

    server = _FakeRemoteServer(
        projects=[
            {"slug": "wp-still-here", "title": "Still Here", "phase": "delivery"}
        ],
        ready=[
            {
                "slug": "tk-still-here",
                "title": "Still Ready",
                "priority": "p1",
                "status": "open",
            }
        ],
        sessions_by_project={},
        raises={"task_for_branch"},
    )

    text = ctx_module.inject_context_remote(server, "https://witan.example.org/mcp")

    assert any(name == "task_for_branch" for name, _ in server.calls)
    assert "## In-Flight Branch" not in text
    assert "Still Here" in text
    assert "Still Ready" in text


def test_inject_context_remote_no_repo_skips_project_list(tmp_path, monkeypatch):
    """Mirrors the local path's own gating: outside a detected repo, no
    project list is fetched at all (calling ``workflow_project_list(repo="")``
    would return every repo's active projects, not "none" — see its
    docstring), but ready tasks still resolve to the unscoped set."""
    from witan import context as ctx_module

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("WITAN_CONTEXT_TTL", "0")
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setenv("WITAN_REPO", "")  # explicitly disables repo detection

    server = _FakeRemoteServer(projects=[], ready=[], sessions_by_project={})

    ctx_module.inject_context_remote(server, "https://witan.example.org/mcp")

    assert not any(name == "workflow_project_list" for name, _ in server.calls)
    assert ("task_ready", {"repo": "", "limit": 10000}) in server.calls


def test_inject_context_remote_cache_key_is_the_deployment_url(tmp_path, monkeypatch):
    """The cache key is the deployment URL, not a store path — two different
    deployments must not collide on one cache entry, and a repeat call within
    the TTL must not re-hit the proxy at all."""
    from witan import context as ctx_module

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/ctx-remote-cache")

    server = _FakeRemoteServer(projects=[], ready=[], sessions_by_project={})

    ctx_module.inject_context_remote(server, "https://witan-a.example.org/mcp")
    calls_after_first = len(server.calls)
    ctx_module.inject_context_remote(server, "https://witan-a.example.org/mcp")
    # Same deployment, second call: served from cache, no fresh proxy read.
    assert len(server.calls) == calls_after_first

    ctx_module.inject_context_remote(server, "https://witan-b.example.org/mcp")
    # A different deployment's URL must not read the first one's cache.
    assert len(server.calls) > calls_after_first


@requires_omnigraph
def test_inject_context_surfaces_unread_comment_once(tmp_path, monkeypatch):
    """A comment on a task this identity holds interrupts the prompt — and then
    stops interrupting, because rendering it is what marks it delivered."""
    from witan import context as ctx_module
    from witan import server as srv

    # The rendered-block cache would serve the first render back verbatim and
    # hide whether the second one actually re-decided anything.
    monkeypatch.setenv("WITAN_CONTEXT_TTL", "0")
    store, queries_dir = _setup(tmp_path, monkeypatch, "https://github.com/test/cmt")
    monkeypatch.chdir(_git_repo(tmp_path / "r"))

    # `cfg.author` is resolved at import, so it is the identity `task_claim`
    # records here — not whatever WITAN_AUTHOR was set to afterwards.
    me = srv.cfg.author
    task = _unwrap(srv.task_create)(title="held work", description="x")
    _unwrap(srv.task_claim)(task["slug"])
    _unwrap(srv.task_comment)(slug=task["slug"], text="the premise cannot fire")

    first = ctx_module.inject_context(str(store), queries_dir, None, author=me)
    assert "## ⚠ New Comments on Work You Hold" in first
    assert "the premise cannot fire" in first
    assert me in first

    second = ctx_module.inject_context(str(store), queries_dir, None, author=me)
    assert "New Comments on Work You Hold" not in second

    # A later comment is unread again — the watermark is a timestamp, not a flag.
    _unwrap(srv.task_comment)(slug=task["slug"], text="and here is why")
    third = ctx_module.inject_context(str(store), queries_dir, None, author=me)
    assert "and here is why" in third
    assert "the premise cannot fire" not in third


@requires_omnigraph
def test_inject_context_ignores_comments_on_tasks_you_do_not_hold(
    tmp_path, monkeypatch
):
    from witan import context as ctx_module
    from witan import server as srv

    monkeypatch.setenv("WITAN_CONTEXT_TTL", "0")
    store, queries_dir = _setup(tmp_path, monkeypatch, "https://github.com/test/cmt2")
    monkeypatch.chdir(_git_repo(tmp_path / "r"))

    theirs = _unwrap(srv.task_create)(title="not yours", description="x")
    _unwrap(srv.task_claim)(theirs["slug"], assignee="someone-else")
    _unwrap(srv.task_comment)(slug=theirs["slug"], text="a note for them")

    unclaimed = _unwrap(srv.task_create)(title="nobody holds this", description="x")
    _unwrap(srv.task_comment)(slug=unclaimed["slug"], text="a note for whoever")

    text = ctx_module.inject_context(
        str(store), queries_dir, None, author=srv.cfg.author
    )
    assert "New Comments on Work You Hold" not in text
    assert "a note for them" not in text
    assert "a note for whoever" not in text


@requires_omnigraph
def test_inject_context_without_an_author_skips_the_comment_block(
    tmp_path, monkeypatch
):
    """No identity to match holders against — the block is skipped, and the
    rest of the context is unaffected."""
    from witan import context as ctx_module
    from witan import server as srv

    monkeypatch.setenv("WITAN_CONTEXT_TTL", "0")
    store, queries_dir = _setup(tmp_path, monkeypatch, "https://github.com/test/cmt3")
    monkeypatch.chdir(_git_repo(tmp_path / "r"))

    held = _unwrap(srv.task_create)(title="held work", description="x")
    _unwrap(srv.task_claim)(held["slug"])
    _unwrap(srv.task_comment)(slug=held["slug"], text="unseen")
    _unwrap(srv.task_create)(title="ready work", description="x")

    text = ctx_module.inject_context(str(store), queries_dir, None)
    assert "New Comments on Work You Hold" not in text
    assert "## Ready Tasks" in text
    assert "ready work" in text


@requires_omnigraph
def test_inject_context_does_not_cache_a_render_carrying_comments(
    tmp_path, monkeypatch
):
    """The one-time comment block survives the output cache being ON.

    The sibling tests above set ``WITAN_CONTEXT_TTL=0``, which is exactly the
    production behaviour that hid this: delivery is recorded when the block is
    rendered, so caching that render would re-serve an already-delivered
    comment on every prompt for the rest of the TTL (Copilot review on
    agent-kit#310). Default TTL here, deliberately.
    """
    from witan import context as ctx_module
    from witan import server as srv

    store, queries_dir = _setup(tmp_path, monkeypatch, "https://github.com/test/cch")
    monkeypatch.chdir(_git_repo(tmp_path / "r"))

    me = srv.cfg.author
    task = _unwrap(srv.task_create)(title="held work", description="x")
    _unwrap(srv.task_claim)(task["slug"])
    _unwrap(srv.task_comment)(slug=task["slug"], text="the premise cannot fire")

    first = ctx_module.inject_context(str(store), queries_dir, None, author=me)
    assert "the premise cannot fire" in first

    second = ctx_module.inject_context(str(store), queries_dir, None, author=me)
    assert "New Comments on Work You Hold" not in second

    # ...and the comment-free render IS cached, so skipping the write above
    # costs one round of reads rather than disabling the cache outright.
    repo, branch = ctx_module._cached_repo_and_branch()
    assert ctx_module._read_output_cache(str(store), repo, branch) == second


@requires_omnigraph
def test_inject_context_surfaces_a_comment_that_arrives_out_of_order(
    tmp_path, monkeypatch
):
    """Delivery is tracked by comment slug, not by a `created_at` watermark.

    ``store_merge`` reconciles ``TaskComment`` like any other type, so a comment
    authored earlier against another store can land here after a later one has
    already been shown. A timestamp watermark hid it permanently.
    """
    from witan import context as ctx_module
    from witan import server as srv

    monkeypatch.setenv("WITAN_CONTEXT_TTL", "0")
    store, queries_dir = _setup(tmp_path, monkeypatch, "https://github.com/test/ooo")
    monkeypatch.chdir(_git_repo(tmp_path / "r"))

    me = srv.cfg.author
    task = _unwrap(srv.task_create)(title="held work", description="x")
    _unwrap(srv.task_claim)(task["slug"])
    _unwrap(srv.task_comment)(slug=task["slug"], text="the later comment")

    first = ctx_module.inject_context(str(store), queries_dir, None, author=me)
    assert "the later comment" in first

    # What a merge from another store looks like on arrival: a row whose
    # `created_at` predates one already delivered.
    srv.client.change(
        "mutations.gq",
        "insert_task_comment",
        {
            "slug": "tc-merged-from-elsewhere",
            "task_slug": task["slug"],
            "body": "authored earlier, arrived later",
            "author": "someone-else",
            "created_at": "2000-01-01T00:00:00Z",
        },
    )

    second = ctx_module.inject_context(str(store), queries_dir, None, author=me)
    assert "authored earlier, arrived later" in second
    assert "the later comment" not in second


@requires_omnigraph
def test_inject_context_caps_comments_rendered_per_block(tmp_path, monkeypatch):
    """Comments are append-only and unread ones accumulate, so the block needs a
    count cap to be bounded — and the overflow must be held back for the next
    render rather than marked delivered unseen."""
    from witan import context as ctx_module
    from witan import server as srv

    monkeypatch.setenv("WITAN_CONTEXT_TTL", "0")
    store, queries_dir = _setup(tmp_path, monkeypatch, "https://github.com/test/cap")
    monkeypatch.chdir(_git_repo(tmp_path / "r"))

    me = srv.cfg.author
    task = _unwrap(srv.task_create)(title="held work", description="x")
    _unwrap(srv.task_claim)(task["slug"])

    total = ctx_module._COMMENT_RENDER_LIMIT + 2
    for i in range(total):
        _unwrap(srv.task_comment)(slug=task["slug"], text=f"comment number {i}")

    first = ctx_module.inject_context(str(store), queries_dir, None, author=me)
    shown = [i for i in range(total) if f"comment number {i}" in first]
    assert shown == list(range(ctx_module._COMMENT_RENDER_LIMIT))
    assert "and 2 more unread" in first

    second = ctx_module.inject_context(str(store), queries_dir, None, author=me)
    still = [i for i in range(total) if f"comment number {i}" in second]
    assert still == list(range(ctx_module._COMMENT_RENDER_LIMIT, total))


@requires_omnigraph
def test_inject_context_skips_your_own_checkpoint_comments(tmp_path, monkeypatch):
    """The witan-task skill has the holder leave a `checkpoint ...` comment
    after each durable step. Those are for the next session's claim-time read,
    not for this session's next prompt, so the holder's own are skipped, matched
    by person even though the claim carries a `#session` qualifier and the
    comment does not. Anyone else's comment, checkpoint or not, still shows."""
    from witan import context as ctx_module
    from witan import server as srv

    monkeypatch.setenv("WITAN_CONTEXT_TTL", "0")
    store, queries_dir = _setup(tmp_path, monkeypatch, "https://github.com/test/ckp")
    monkeypatch.chdir(_git_repo(tmp_path / "r"))

    me = srv.cfg.author
    task = _unwrap(srv.task_create)(title="held work", description="x")
    _unwrap(srv.task_claim)(task["slug"], assignee=f"{me}#5e313f6d")
    _unwrap(srv.task_comment)(
        slug=task["slug"],
        text="checkpoint pushed: tm/x @ 3f2a1c9 | next: gh pr create --fill-first",
    )
    _unwrap(srv.task_comment)(slug=task["slug"], text="my own plain note")
    srv.client.change(
        "mutations.gq",
        "insert_task_comment",
        {
            "slug": "tc-teammate-checkpoint",
            "task_slug": task["slug"],
            "body": "checkpoint blocked: waiting on infra | next: ask in #ops",
            "author": "someone-else",
            "created_at": "2030-01-01T00:00:00Z",
        },
    )

    text = ctx_module.inject_context(str(store), queries_dir, None, author=me)
    assert "New Comments on Work You Hold" in text
    assert "checkpoint pushed" not in text
    assert "my own plain note" in text
    assert "checkpoint blocked: waiting on infra" in text


@pytest.mark.parametrize(
    ("author", "holder", "skipped"),
    [
        ("alice", "alice#5e313f6d", True),
        ("alice", "alice", True),
        ("bob", "alice#5e313f6d", False),
        # An identity is free-form, so it can end in something qualifier-shaped.
        # Only the holder side may be stripped.
        ("alice#ops", "alice#5e313f6d", False),
        ("alice#ops", "alice#ops#5e313f6d", True),
        ("alice#ops", "alice#ops", True),
        (None, "alice#5e313f6d", False),
    ],
)
def test_own_checkpoint_matches_the_author_against_the_holder(author, holder, skipped):
    from witan import context as ctx_module

    comment = {"author": author, "body": "checkpoint pushed: x @ 3f2a1c9 | next: y"}
    assert ctx_module._is_own_checkpoint(comment, holder) is skipped


def test_held_by_matches_an_unqualified_claim_by_a_hash_identity():
    from witan import context as ctx_module

    rows = [
        {"slug": "tk-a", "status": "in_progress", "assignee": "alice#ops"},
        {"slug": "tk-b", "status": "in_progress", "assignee": "alice#ops#5e313f6d"},
        {"slug": "tk-c", "status": "in_progress", "assignee": "alice#5e313f6d"},
    ]
    held = ctx_module._held_by(rows, "alice#ops")
    assert [r["slug"] for r in held] == ["tk-a", "tk-b"]


def test_inject_context_remote_skips_your_own_checkpoint_comments(
    tmp_path, monkeypatch
):
    """Same filter on the deployment path, where the holder string comes from
    the `@me` read rather than a local `cfg.author`."""
    from witan import context as ctx_module

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("WITAN_CONTEXT_TTL", "0")
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/ckp-remote")

    held = {
        "slug": "tk-held",
        "title": "held work",
        "status": "in_progress",
        "assignee": "alice#5e313f6d",
    }
    server = _FakeRemoteServer(
        projects=[],
        ready=[],
        sessions_by_project={},
        held=[held],
        comments={
            "tk-held": [
                {
                    "slug": "tc-1",
                    "author": "alice",
                    "body": "checkpoint validated: just test-witan-council | next: push",
                },
                {"slug": "tc-2", "author": "bob", "body": "checkpoint from bob"},
                {"slug": "tc-3", "author": "alice", "body": "a plain note"},
            ]
        },
    )
    text = ctx_module.inject_context_remote(server, "https://witan.example.org/mcp")

    assert "checkpoint validated" not in text
    assert "bob: checkpoint from bob" in text
    assert "alice: a plain note" in text


def test_inject_context_remote_asks_for_held_tasks_across_all_repos(
    tmp_path, monkeypatch
):
    """`task_list` is a repo-scoped tool, so an OMITTED repo is filled in with
    the current checkout's by `RemoteMCPProxy._map_args` — which would make the
    remote path miss comments on held tasks in other repos, while the local path
    scans every repo. The explicit sentinel keeps both answering the same
    question.

    The explicit `limit` is part of the same question. `assignee` is applied
    in Python after the read and an unscoped read is capped at 50 in the
    query, so without it your own held tasks can fall outside the newest 50
    `in_progress` rows and this block comes back empty for an unrelated
    reason."""
    from witan import context as ctx_module

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("WITAN_CONTEXT_TTL", "0")
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/held-scope")

    server = _FakeRemoteServer(projects=[], ready=[], sessions_by_project={})
    ctx_module.inject_context_remote(server, "https://witan.example.org/mcp")

    assert (
        "task_list",
        {"assignee": "@me", "status": "in_progress", "repo": "", "limit": 10000},
    ) in server.calls


def test_inject_context_remote_issues_its_independent_reads_concurrently(
    tmp_path, monkeypatch
):
    """The cold path is the slowest read, not the sum of them.

    Issued one at a time, the hook's ten round trips against a deployment took
    ~11s measured and blew the 15s timeout it installs (agent-kit#349). Each
    read here blocks on a barrier that only releases once every first-wave
    call has arrived, so a sequential implementation times out on the first
    read rather than quietly passing slower.
    """
    from witan import context as ctx_module

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("WITAN_CONTEXT_TTL", "0")
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)
    repo = "https://github.com/test/ctx-concurrent"
    monkeypatch.setenv("WITAN_REPO", repo)
    monkeypatch.setattr(ctx_module, "_current_branch", lambda: "main")

    # The four first-wave reads. The second wave (sessions, comments) depends
    # on their answers, so it is deliberately not held here.
    first_wave = {
        "workflow_project_list",
        "task_ready",
        "task_for_branch",
        "task_list",
    }
    barrier = asyncio.Barrier(len(first_wave))

    class _Barriered(_FakeRemoteServer):
        async def dispatch(self, name, /, **kwargs):
            if name in first_wave:
                await asyncio.wait_for(barrier.wait(), timeout=10)
            return await super().dispatch(name, **kwargs)

    server = _Barriered(
        projects=[{"slug": "wp-x", "title": "X", "phase": "spec"}],
        ready=[],
        sessions_by_project={},
        branch_tasks=[],
        held=[],
    )
    text = ctx_module.inject_context_remote(
        server, "https://witan.example.org/mcp", debug=True
    )

    assert "## Active Workflow Projects" in text
    assert "partial" not in text


def test_inject_context_remote_primes_the_tool_schema_once_before_fanning_out(
    tmp_path, monkeypatch
):
    """One schema resolution per hook call, ahead of the first wave.

    Without it each concurrent read finds the param-name cache unset and lists
    the surface itself (measured: 4 `tools/list` cold, where the sequential
    path issued 1).
    """
    from witan import context as ctx_module

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("WITAN_CONTEXT_TTL", "0")
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/ctx-prime")

    class _Ordered(_FakeRemoteServer):
        async def ensure_tool_schema(self):
            assert not self.calls, "primed after a read went out"
            await super().ensure_tool_schema()

    server = _Ordered(projects=[], ready=[], sessions_by_project={})
    ctx_module.inject_context_remote(server, "https://witan.example.org/mcp")
    assert server.primed == 1


def test_inject_context_remote_survives_a_failed_prime(tmp_path, monkeypatch):
    """Priming is an optimisation; each read resolves the schema itself."""
    from witan import context as ctx_module

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("WITAN_CONTEXT_TTL", "0")
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/ctx-prime-fail")

    class _BadPrime(_FakeRemoteServer):
        async def ensure_tool_schema(self):
            raise RuntimeError("tools/list failed")

    server = _BadPrime(
        projects=[],
        ready=[{"slug": "tk-r", "title": "Still ready", "priority": "p1"}],
        sessions_by_project={},
    )
    text = ctx_module.inject_context_remote(server, "https://witan.example.org/mcp")

    assert "Still ready" in text


def test_gather_reports_each_failure_against_its_own_key():
    from witan import context as ctx_module

    async def _fine():
        return "fine"

    async def _boom():
        raise ValueError("nope")

    got = ctx_module._gather(
        {"ok": _fine, "bad": _boom}, deadline=time.monotonic() + 10
    )

    assert got["ok"] == "fine"
    assert isinstance(got["bad"], ValueError)
    assert not isinstance(got["bad"], ctx_module._TimedOut)


def test_gather_keeps_what_answered_and_cancels_the_rest():
    """The reason for ``asyncio.wait`` over a timed-out ``gather``: the reads
    that came back before the deadline are kept, not thrown away with it."""
    from witan import context as ctx_module

    cancelled = []

    async def _fast():
        return "fast"

    async def _hung():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    started = time.monotonic()
    got = ctx_module._gather(
        {"fast": _fast, "hung": _hung}, deadline=time.monotonic() + 0.5
    )

    assert time.monotonic() - started < 1.5
    assert got["fast"] == "fast"
    assert isinstance(got["hung"], ctx_module._TimedOut)
    assert cancelled == [True]


def test_gather_returns_at_the_deadline_when_a_call_blocks_the_loop():
    """The proxy's token provider is synchronous and runs on the loop, so a
    stalled refresh blocks it and ``asyncio.wait``'s timer cannot fire. The
    hook must still get control back at the deadline."""
    from witan import context as ctx_module

    async def _blocks_the_loop():
        time.sleep(3)  # noqa: ASYNC251 — blocking the loop is the case under test
        return "too late"

    started = time.monotonic()
    got = ctx_module._gather(
        {"blocked": _blocks_the_loop}, deadline=time.monotonic() + 0.5
    )

    assert time.monotonic() - started < 1.5
    assert isinstance(got["blocked"], ctx_module._TimedOut)


def test_gather_keeps_an_answer_that_lands_just_before_the_loop_blocks():
    """A done callback runs on a later loop turn, so recording from one lost a
    read that finished right before another blocked the loop: the fast read
    here completes, then the blocking one runs before any callback could."""
    from witan import context as ctx_module

    async def _fast():
        return "fast"

    async def _fails():
        raise ValueError("read failed")

    async def _blocks_the_loop():
        time.sleep(3)  # noqa: ASYNC251 — blocking the loop is the case under test
        return "too late"

    got = ctx_module._gather(
        {"fast": _fast, "bad": _fails, "blocked": _blocks_the_loop},
        deadline=time.monotonic() + 0.5,
    )

    assert got["fast"] == "fast"
    assert isinstance(got["bad"], ValueError)
    assert isinstance(got["blocked"], ctx_module._TimedOut)


def test_inject_context_remote_survives_a_second_wave_that_cannot_start(
    tmp_path, monkeypatch
):
    """At its thread limit the process cannot start a wave's loop thread. The
    second wave runs outside the remote path's handler, so ``_gather`` must
    report that per read rather than raise and lose the first wave's blocks."""
    import threading

    from witan import context as ctx_module

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("WITAN_CONTEXT_TTL", "0")
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/ctx-no-threads")

    started = []
    real_thread = threading.Thread

    class _ThirdStartFails(real_thread):
        # Prime, first wave, second wave: the third start is the second wave.
        def start(self):
            started.append(self.name)
            if len(started) == 3:
                raise RuntimeError("can't start new thread")
            super().start()

    monkeypatch.setattr(ctx_module.threading, "Thread", _ThirdStartFails)

    server = _FakeRemoteServer(
        projects=[{"slug": "wp-x", "title": "First Wave Project", "phase": "spec"}],
        ready=[{"slug": "tk-r", "title": "First Wave Ready", "priority": "p1"}],
        sessions_by_project={},
        held=[{"slug": "tk-h", "title": "Held", "status": "in_progress"}],
    )
    text = ctx_module.inject_context_remote(
        server, "https://witan.example.org/mcp", debug=True
    )

    assert len(started) == 3
    assert "First Wave Project" in text
    assert "First Wave Ready" in text
    # Not a timeout, so no partial line, and no second-wave read went out.
    assert "partial" not in text
    assert not [
        c for c in server.calls if c[0] in ("workflow_session_list", "task_get")
    ]


def test_gather_past_its_deadline_starts_nothing():
    from witan import context as ctx_module

    started = []

    async def _read():
        started.append(True)
        return "late"

    got = ctx_module._gather({"late": _read}, deadline=time.monotonic() - 1)

    assert isinstance(got["late"], ctx_module._TimedOut)
    assert not started


def _deadline_env(tmp_path, monkeypatch, repo):
    """A 1s deadline with the output cache ON, so a test can see it unwritten."""
    from witan import context as ctx_module

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setenv("WITAN_REPO", repo)
    monkeypatch.setenv("WITAN_CONTEXT_DEADLINE", "1")
    monkeypatch.setattr(ctx_module, "_current_branch", lambda: "main")


def test_inject_context_remote_renders_what_answered_by_the_deadline(
    tmp_path, monkeypatch
):
    """One read the deployment never answers costs its own block, not the hook.

    Before the deadline, a hung read held the hook for the MCP client's 300s
    read timeout, and Claude Code killed it at 45s with nothing rendered
    (agent-kit#349). Now the projects read is cancelled at the deadline and the
    ready tasks that did answer still render, with a line saying what is
    missing. That render is not cached, so the next prompt retries in full.
    """
    from witan import context as ctx_module

    repo = "https://github.com/test/ctx-deadline-first"
    _deadline_env(tmp_path, monkeypatch, repo)
    url = "https://witan.example.org/mcp"

    server = _FakeRemoteServer(
        projects=[{"slug": "wp-x", "title": "Never Shown", "phase": "spec"}],
        ready=[{"slug": "tk-r", "title": "Ready one", "priority": "p1"}],
        sessions_by_project={},
        hangs={"workflow_project_list"},
    )
    started = time.monotonic()
    text = ctx_module.inject_context_remote(server, url, debug=True)

    assert time.monotonic() - started < 2
    assert "## Ready Tasks" in text
    assert "Ready one" in text
    assert "## Active Workflow Projects" not in text
    assert "witan context partial: projects timed out after 1s" in text
    assert ctx_module._read_output_cache(url, repo, "main") is None


def test_inject_context_remote_second_wave_spends_what_the_first_left(
    tmp_path, monkeypatch
):
    """The deadline covers both waves, not each one: a hung session read in
    wave two is cut off at the same 1s mark, and the first wave's blocks
    render without the resume lines it would have fed."""
    from witan import context as ctx_module

    repo = "https://github.com/test/ctx-deadline-second"
    _deadline_env(tmp_path, monkeypatch, repo)
    url = "https://witan.example.org/mcp"

    server = _FakeRemoteServer(
        projects=[{"slug": "wp-x", "title": "Shown Project", "phase": "spec"}],
        ready=[{"slug": "tk-r", "title": "Ready one", "priority": "p1"}],
        sessions_by_project={},
        hangs={"workflow_session_list"},
    )
    started = time.monotonic()
    text = ctx_module.inject_context_remote(server, url, debug=True)

    assert time.monotonic() - started < 2
    assert ("workflow_session_list", {"project_slug": "wp-x"}) in server.calls
    assert "Shown Project" in text
    assert "Ready one" in text
    assert "witan context partial: project sessions timed out after 1s" in text
    assert ctx_module._read_output_cache(url, repo, "main") is None


def test_inject_context_remote_failed_ready_read_still_blanks_the_block(
    tmp_path, monkeypatch
):
    """A projects/ready read that FAILS keeps its old contract (empty block);
    only one that runs out of time renders partially. An error answer is not
    a slow one, and nothing says the next prompt will fare better."""
    from witan import context as ctx_module

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("WITAN_CONTEXT_TTL", "0")
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/ctx-ready-fails")

    server = _FakeRemoteServer(
        projects=[{"slug": "wp-x", "title": "X", "phase": "spec"}],
        ready=[],
        sessions_by_project={},
        raises={"task_ready"},
    )

    assert (
        ctx_module.inject_context_remote(server, "https://witan.example.org/mcp") == ""
    )


def test_context_deadline_fits_inside_the_hook_timeout(monkeypatch):
    """The deadline is only worth having if it fires before the client kills
    the hook; past ``INJECT_CONTEXT_TIMEOUT_SECONDS`` the output is discarded."""
    from witan import context as ctx_module
    from witan.setup import INJECT_CONTEXT_TIMEOUT_SECONDS

    monkeypatch.delenv("WITAN_CONTEXT_DEADLINE", raising=False)
    assert ctx_module._context_deadline() < INJECT_CONTEXT_TIMEOUT_SECONDS

    monkeypatch.setenv("WITAN_CONTEXT_DEADLINE", "12.5")
    assert ctx_module._context_deadline() == 12.5
    monkeypatch.setenv("WITAN_CONTEXT_DEADLINE", "soon")
    assert ctx_module._context_deadline() == ctx_module._CONTEXT_DEADLINE


def test_inject_context_remote_one_failed_read_costs_only_its_own_block(
    tmp_path, monkeypatch
):
    """Per-read isolation must survive the move to concurrent waves.

    A read that fails inside a wave comes back as a value rather than a live
    exception, so the block it feeds is the only thing that may disappear.
    """
    from witan import context as ctx_module

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("WITAN_CONTEXT_TTL", "0")
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)
    repo = "https://github.com/test/ctx-isolated"
    monkeypatch.setenv("WITAN_REPO", repo)
    monkeypatch.setattr(ctx_module, "_current_branch", lambda: "main")

    server = _FakeRemoteServer(
        projects=[{"slug": "wp-x", "title": "X", "phase": "spec"}],
        ready=[{"slug": "tk-r", "title": "Ready one", "priority": "p1"}],
        sessions_by_project={},
        branch_tasks=[{"slug": "tk-b", "title": "Branch one", "status": "open"}],
        raises=("task_for_branch",),
    )
    text = ctx_module.inject_context_remote(
        server, "https://witan.example.org/mcp", debug=True
    )

    assert "## In-Flight Branch" not in text
    assert "## Active Workflow Projects" in text
    assert "## Ready Tasks" in text
    assert "Ready one" in text
