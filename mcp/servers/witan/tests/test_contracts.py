"""Tests for recall's contract and memory->symbol sections (spec §4).

witan-code is not a dependency of this package, so the cross-store halves
(bridge bindings, live symbol definitions) degrade. These tests cover the
Layer-1 contract-anchor side, the graceful degradation, and, with a stand-in
for witan-code's async tools, that a definition or binding is awaited rather
than returned as a coroutine.
"""

import types

import pytest

from .conftest import requires_omnigraph


def _contract(server, key, **kwargs):
    return server.recall(
        contract=key, hops=0, include_superseded=True, limit=100, **kwargs
    )


@requires_omnigraph
def test_recall_by_contract_returns_tagged_memories(server):
    m = server.memory_store(
        kind="project_fact", title="db", content="service uses postgres"
    )
    server.memory_link(m["slug"], "DATABASE_URL:contract", "tagged")

    out = _contract(server, "DATABASE_URL")
    assert m["slug"] in {x["slug"] for x in out["memories"]}
    assert out["seeds"]["contract"] == [m["slug"]]
    section = out["contract"]
    assert section["key_norm"] == "DATABASE_URL"
    assert section["topic"]["name"] == "DATABASE_URL"
    # bridge bindings degrade to empty without witan-code, but the shape is stable
    assert section["bindings"] == {"providers": [], "consumers": []}


@requires_omnigraph
def test_recall_by_contract_is_cross_repo(server):
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

    slugs = {x["slug"] for x in _contract(server, "GET /api/v1/courses/")["memories"]}
    assert {a["slug"], b["slug"]} <= slugs


@requires_omnigraph
def test_recall_by_unknown_contract_is_empty(server):
    out = _contract(server, "NONEXISTENT_KEY", contract_kind="env_var")
    assert out["memories"] == []
    assert out["contract"] == {
        "key_norm": "NONEXISTENT_KEY",
        "kind": "env_var",
        "topic": None,
        "bindings": {"providers": [], "consumers": []},
    }


@pytest.fixture
def code(monkeypatch):
    """A stand-in witan-code whose read tools are ``async``, like the real ones."""
    from witan import server as srv

    calls: list[tuple] = []

    async def code_find_definition(symbol_id):
        calls.append(("definition", symbol_id))
        return [{"symbol_id": symbol_id}]

    async def code_interface_providers(kind, key):
        calls.append(("providers", kind, key))
        return [{"repo": "https://github.com/example/infra"}]

    async def code_interface_consumers(kind, key):
        calls.append(("consumers", kind, key))
        return [{"repo": "https://github.com/example/web"}]

    stub = types.SimpleNamespace(
        code_find_definition=code_find_definition,
        code_interface_providers=code_interface_providers,
        code_interface_consumers=code_interface_consumers,
        calls=calls,
    )
    monkeypatch.setattr(srv._code_server, "_cached", stub, raising=False)
    return stub


@requires_omnigraph
def test_recall_by_contract_awaits_the_bindings(server, code):
    out = _contract(server, "DATABASE_URL", contract_kind="env_var")

    assert out["contract"]["bindings"] == {
        "providers": [{"repo": "https://github.com/example/infra"}],
        "consumers": [{"repo": "https://github.com/example/web"}],
    }
    assert ("providers", "env_var", "DATABASE_URL") in code.calls


@requires_omnigraph
def test_recall_by_contract_without_a_kind_skips_the_bridge(server, code):
    out = _contract(server, "DATABASE_URL")

    assert out["contract"]["bindings"] == {"providers": [], "consumers": []}
    assert code.calls == []


@requires_omnigraph
def test_memory_symbols_await_the_definition(server, code):
    ref = "https://github.com/test/repo#app/models.py::Course"
    m = server.memory_store(
        kind="lesson", title="t", content="c", severity="info", symbol_refs=[ref]
    )

    (symbol,) = server.recall(memory=m["slug"], hops=0)["memory"]["symbols"]

    assert symbol == {
        "symbol_ref": ref,
        "definition": [{"symbol_id": ref}],
    }


@requires_omnigraph
def test_memory_symbols_degrade_to_raw_refs(server):
    ref = "https://github.com/test/repo#app/models.py::Course"
    m = server.memory_store(
        kind="lesson",
        title="model lesson",
        content="careful with Course",
        severity="warning",
        symbol_refs=[ref],
    )
    symbols = server.recall(memory=m["slug"], hops=0)["memory"]["symbols"]
    assert [s["symbol_ref"] for s in symbols] == [ref]
    assert "definition" not in symbols[0]  # no witan-code → raw ref only


@requires_omnigraph
def test_memory_section_for_a_missing_memory_is_empty(server):
    out = server.recall(memory="pat-nope", hops=0)
    assert out["memories"] == []
    assert out["memory"]["symbols"] == []
    assert all(rows == [] for rows in out["memory"]["neighbors"].values())


@requires_omnigraph
def test_memory_symbols_skip_non_symbol_refs(server):
    # A ref without the :: delimiter isn't a symbol id — it still round-trips as a
    # raw ref and never trips the resolver.
    m = server.memory_store(
        kind="lesson",
        title="weird",
        content="x",
        severity="info",
        symbol_refs=["not-a-symbol-id", "https://github.com/x/y#a.py::Z"],
    )
    symbols = server.recall(memory=m["slug"], hops=0)["memory"]["symbols"]
    refs = [s["symbol_ref"] for s in symbols]
    assert refs == ["not-a-symbol-id", "https://github.com/x/y#a.py::Z"]
    assert all("definition" not in s for s in symbols)  # witan-code absent


@requires_omnigraph
def test_code_server_absent_is_cached_none(server):
    from witan import server as srv

    # witan-code isn't a dependency of this package, so this resolves (and caches)
    # to None without raising.
    assert srv._code_server() is None
    assert srv._code_server() is None
