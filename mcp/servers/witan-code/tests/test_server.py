"""End-to-end tests for the codegraph MCP tools."""

import asyncio
import inspect

from .conftest import requires_stack


def _fn(tool):
    """Unwrap a FastMCP-decorated tool to a directly-callable function.

    Tools that gained MCP elicitation are ``async def`` (they take a
    ``ctx: Context`` FastMCP injects). Calls in this file invoke tools
    directly, not through an MCP client, so run a coroutine tool to
    completion via ``asyncio.run`` — with no ctx it falls back to its
    non-interactive default, matching pre-elicitation behavior.
    """
    fn = getattr(tool, "fn", tool)
    if inspect.iscoroutinefunction(fn):

        def runner(*args, **kwargs):
            return asyncio.run(fn(*args, **kwargs))

        return runner
    return fn


@requires_stack
def test_reindex_then_query_tools(sample_repo, monkeypatch):
    from witan_code import config as cfg_mod
    from witan_code import server as srv

    # server.cfg was captured at import; refresh it for the test's env + store.
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()

    stats = _fn(srv.code_reindex)(path=str(sample_repo))
    assert stats["symbols"] >= 3
    assert stats["errors"] == 0

    defs = _fn(srv.code_find_definition)("run")
    assert any(d["qualified_name"] == "Service.run" for d in defs)

    helper_def = _fn(srv.code_find_definition)("helper")
    assert helper_def
    # The id round-trips under one name: definitions expose `symbol_id`, which
    # is exactly what the id-routed tools consume (no `slug` on symbol output).
    helper_id = helper_def[0]["symbol_id"]
    assert "slug" not in helper_def[0]
    callers = _fn(srv.code_callers)(helper_id)
    assert "Service.run" in {c["qualified_name"] for c in callers}
    assert all("symbol_id" in c and "slug" not in c for c in callers)

    impact = _fn(srv.code_impact)(helper_id)
    assert impact["root"] == helper_id
    assert "Service.run" in {s["qualified_name"] for s in impact["impacted"]}

    hits = _fn(srv.code_search_symbol)("helper")
    assert any(h["name"] == "helper" for h in hits)
    assert all("symbol_id" in h for h in hits)


@requires_stack
def test_tools_return_empty_without_store(monkeypatch, tmp_path):
    # Pointed at a repo that was never indexed -> graceful empty results.
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/never-indexed")
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "empty"))
    from witan_code import config as cfg_mod
    from witan_code import server as srv

    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()
    assert _fn(srv.code_find_definition)("anything") == []


# ── Store/bridge introspection tools ──────────────────────────────────────────
# These back `witan-code repos` / `branches` / `symbols` / `deps`, which route
# through the tool surface so they work against a deployment too (ADR 0005).


@requires_stack
def test_indexed_repos_and_branches_report_the_store(sample_repo, monkeypatch):
    from witan_code import config as cfg_mod
    from witan_code import server as srv

    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()
    _fn(srv.code_reindex)(path=str(sample_repo))

    repos = _fn(srv.code_indexed_repos)()
    assert [r["repo"] for r in repos] == ["https://github.com/test/cg"]
    assert repos[0]["files"] >= 1
    assert repos[0]["bytes"] > 0
    assert repos[0]["last_indexed"] > 0

    branches = _fn(srv.code_indexed_branches)()
    assert [b["repo"] for b in branches] == ["https://github.com/test/cg"]
    assert "main" in branches[0]["branches"]


@requires_stack
def test_indexed_repos_is_empty_without_any_store(monkeypatch, tmp_path):
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "empty"))
    from witan_code import config as cfg_mod
    from witan_code import server as srv

    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()
    assert _fn(srv.code_indexed_repos)() == []
    assert _fn(srv.code_indexed_branches)() == []


@requires_stack
def test_repo_symbols_and_dependencies_read_the_bridge(sample_repo, monkeypatch):
    from witan_code import config as cfg_mod
    from witan_code import server as srv

    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()
    srv._git_context.clear()
    _fn(srv.code_reindex)(path=str(sample_repo))

    rows = _fn(srv.code_repo_symbols)(repo="https://github.com/test/cg")
    assert all(r["repo"] == "https://github.com/test/cg" for r in rows)
    # Filters compose without touching the store's own query.
    exported = _fn(srv.code_repo_symbols)(
        repo="https://github.com/test/cg", role="exported"
    )
    assert all(r["role"] == "exported" for r in exported)

    deps = _fn(srv.code_repo_dependencies)()
    assert set(deps) == {"repos", "edges"}
    assert isinstance(deps["edges"], list)


@requires_stack
def test_repo_symbols_without_a_repo_is_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("WITAN_REPO", "")
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "empty"))
    monkeypatch.chdir(tmp_path)
    from witan_code import config as cfg_mod
    from witan_code import server as srv

    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()
    srv._git_context.clear()
    assert _fn(srv.code_repo_symbols)() == []
