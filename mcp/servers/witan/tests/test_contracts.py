"""Tests for memory↔contract and memory→symbol forward links (spec §4).

witan-code is not a dependency of this package, so the cross-store halves
(bridge bindings, live symbol definitions) degrade — these tests cover the
Layer-1 contract-anchor side and the graceful degradation.
"""

from .conftest import requires_omnigraph


@requires_omnigraph
def test_memory_for_contract_returns_tagged_memories(server):
    m = server.memory_store(
        kind="project_fact", title="db", content="service uses postgres"
    )
    server.memory_link(m["slug"], "DATABASE_URL:contract", "tagged")

    out = server.memory_for_contract("DATABASE_URL")
    assert m["slug"] in {x["slug"] for x in out["memories"]}
    # bridge bindings degrade to empty without witan-code, but the shape is stable
    assert out["bindings"] == {"providers": [], "consumers": []}


@requires_omnigraph
def test_memory_for_contract_is_cross_repo(server):
    a = server.memory_store(
        kind="project_fact",
        title="producer",
        content="emits the event",
        repo="https://github.com/test/producer",
    )
    b = server.memory_store(
        kind="project_fact",
        title="consumer",
        content="reads the event",
        repo="https://github.com/test/consumer",
    )
    for mem in (a, b):
        server.memory_link(mem["slug"], "GET /api/v1/courses/:contract", "tagged")

    slugs = {
        x["slug"]
        for x in server.memory_for_contract("GET /api/v1/courses/")["memories"]
    }
    assert {a["slug"], b["slug"]} <= slugs


@requires_omnigraph
def test_memory_for_unknown_contract_is_empty(server):
    out = server.memory_for_contract("NONEXISTENT_KEY", kind="env_var")
    assert out["memories"] == []
    assert out["bindings"] == {"providers": [], "consumers": []}


@requires_omnigraph
def test_memory_symbol_context_degrades_to_raw_refs(server):
    ref = "https://github.com/test/repo#app/models.py::Course"
    m = server.memory_store(
        kind="lesson",
        title="model lesson",
        content="careful with Course",
        severity="warning",
        symbol_refs=[ref],
    )
    out = server.memory_symbol_context(m["slug"])
    assert [s["symbol_ref"] for s in out["symbols"]] == [ref]
    assert "definition" not in out["symbols"][0]  # no witan-code → raw ref only


@requires_omnigraph
def test_memory_symbol_context_missing_memory(server):
    assert server.memory_symbol_context("pat-nope")["symbols"] == []


@requires_omnigraph
def test_memory_symbol_context_skips_non_symbol_refs(server):
    # A ref without the :: delimiter isn't a symbol id — it still round-trips as a
    # raw ref and never trips the resolver.
    m = server.memory_store(
        kind="lesson",
        title="weird",
        content="x",
        severity="info",
        symbol_refs=["not-a-symbol-id", "https://github.com/x/y#a.py::Z"],
    )
    out = server.memory_symbol_context(m["slug"])
    refs = [s["symbol_ref"] for s in out["symbols"]]
    assert refs == ["not-a-symbol-id", "https://github.com/x/y#a.py::Z"]
    assert all("definition" not in s for s in out["symbols"])  # witan-code absent


@requires_omnigraph
def test_code_server_absent_is_cached_none(server):
    from witan import server as srv

    # witan-code isn't a dependency of this package, so this resolves (and caches)
    # to None without raising.
    assert srv._code_server() is None
    assert srv._code_server() is None
