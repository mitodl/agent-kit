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
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "WITAN_CONFIG": str(cfg_file),
            "WITAN_TARGET": "does-not-exist",
            "WITAN_CONTEXT_TTL": "not-a-number",
            "WITAN_REPO": "",
            # DEBUG so the debug-level degradations are emitted rather than
            # filtered out, which would make this pass vacuously.
            "WITAN_LOG_LEVEL": "DEBUG",
        },
    )

    assert result.stdout == "", f"hook wrote to stdout: {result.stdout!r}"
    # And it genuinely reported the degradations rather than being silent.
    assert "witan." in result.stderr
