"""End-to-end tests for the codegraph MCP tools."""

from .conftest import requires_stack


def _fn(tool):
    return getattr(tool, "fn", tool)


@requires_stack
def test_reindex_then_query_tools(sample_repo, monkeypatch):
    from omnigraph_codegraph import config as cfg_mod
    from omnigraph_codegraph import server as srv

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
    callers = _fn(srv.code_callers)(helper_def[0]["slug"])
    assert "Service.run" in {c["qualified_name"] for c in callers}

    impact = _fn(srv.code_impact)(helper_def[0]["slug"])
    assert impact["root"] == helper_def[0]["slug"]
    assert "Service.run" in {s["qualified_name"] for s in impact["impacted"]}

    hits = _fn(srv.code_search_symbol)("helper")
    assert any(h["name"] == "helper" for h in hits)


@requires_stack
def test_tools_return_empty_without_store(monkeypatch, tmp_path):
    # Pointed at a repo that was never indexed -> graceful empty results.
    monkeypatch.setenv(
        "OMNIGRAPH_CODEGRAPH_REPO", "https://github.com/test/never-indexed"
    )
    monkeypatch.setenv("OMNIGRAPH_CODEGRAPH_DIR", str(tmp_path / "empty"))
    from omnigraph_codegraph import config as cfg_mod
    from omnigraph_codegraph import server as srv

    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()
    assert _fn(srv.code_find_definition)("anything") == []
