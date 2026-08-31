"""Tests for the UserPromptSubmit context-injection hook (witan/context.py),
focused on the CodeBranch "In-Flight Branch" section, and on the
`witan inject-context` command never failing the hook."""

import logging
import os
import subprocess
import sys
import textwrap

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

    Stands in for the tool-calling proxy ``_srv()``/``remote_proxy()`` build —
    ``inject_context_remote`` only ever calls attribute-style methods on it, the
    same shape ``RemoteServerProxy.__getattr__`` returns, so a plain class with
    matching method names is a faithful substitute without dialling out.
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
    ):
        self.calls: list[tuple[str, dict]] = []
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


def test_inject_context_remote_asks_for_held_tasks_across_all_repos(
    tmp_path, monkeypatch
):
    """`task_list` is a repo-scoped tool, so an OMITTED repo is filled in with
    the current checkout's by `RemoteMCPProxy._map_args` — which would make the
    remote path miss comments on held tasks in other repos, while the local path
    scans every repo. The explicit sentinel keeps both answering the same
    question."""
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
        {"assignee": "@me", "status": "in_progress", "repo": ""},
    ) in server.calls
