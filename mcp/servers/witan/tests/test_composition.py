"""Tests for the Layer-1 <-> Layer-2 composition via symbol references."""

from .conftest import requires_omnigraph

SID = "https://github.com/test/repo#app/svc.py::Service.run"


@requires_omnigraph
def test_context_for_symbol_finds_memory_and_task(server):
    mem = server.memory_store(
        kind="lesson",
        title="watch Service.run",
        content="careful here",
        severity="warning",
        symbol_refs=[SID],
    )
    proj = server.workflow_project_create(title="P", description="d")
    tk = server.task_create(
        title="refactor Service.run",
        description="y",
        project_slug=proj["slug"],
        symbol_refs=[SID],
    )

    ctx = server.context_for_symbol(SID)
    assert ctx["symbol_id"] == SID
    assert mem["slug"] in {m["slug"] for m in ctx["memories"]}
    assert tk["slug"] in {t["slug"] for t in ctx["tasks"]}


@requires_omnigraph
def test_context_for_unrelated_symbol_is_empty(server):
    server.memory_store(kind="lesson", title="x", content="y", symbol_refs=[SID])
    ctx = server.context_for_symbol(SID + "::other")
    assert ctx["memories"] == []
    assert ctx["tasks"] == []
