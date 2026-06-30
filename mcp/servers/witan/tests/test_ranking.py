"""Tests for the composite memory re-rank (spec §7).

The scoring math is unit-tested directly via ``_score`` (equal norm_bm25, vary
one term) since the engine can't project a real BM25 score for an end-to-end
tie. Plumbing and the BM25-preserving degenerate case are tested through the
public tools.
"""

import pytest

from witan.config import RankConfig

from .conftest import requires_omnigraph

_DEFAULT = RankConfig()


def _score(server, **overrides):
    base = dict(
        norm_bm25=0.5,
        age_days=0.0,
        corroboration=0,
        confidence=None,
        is_superseded=False,
        is_contradicted=False,
        rank_cfg=_DEFAULT,
    )
    base.update(overrides)
    return server._score(**base)


@requires_omnigraph
def test_recency_raises_score(server):
    assert _score(server, age_days=0.0) > _score(server, age_days=365.0)


@requires_omnigraph
def test_corroboration_raises_score(server):
    assert _score(server, corroboration=5) > _score(server, corroboration=0)


@requires_omnigraph
def test_higher_confidence_ranks_higher(server):
    assert _score(server, confidence=0.9) > _score(server, confidence=0.1)


@requires_omnigraph
def test_recency_weight_zero_removes_age_effect(server):
    cfg0 = RankConfig(w_recency=0.0)
    young = _score(server, age_days=0.0, confidence=0.6, rank_cfg=cfg0)
    ancient = _score(server, age_days=9999.0, confidence=0.6, rank_cfg=cfg0)
    assert young == ancient


@requires_omnigraph
def test_superseded_and_contradicted_penalised(server):
    base = _score(server)
    assert _score(server, is_superseded=True) < base
    assert _score(server, is_contradicted=True) < base


@requires_omnigraph
def test_confidence_round_trips_through_store_and_search(server):
    res = server.memory_store(
        kind="pattern", title="conf", content="distinctive zorp", confidence=0.9
    )
    node = server.memory_get(res["slug"])
    assert node["confidence"] == pytest.approx(0.9, abs=1e-6)

    hits = server.memory_search("distinctive zorp")
    match = next(h for h in hits if h["slug"] == res["slug"])
    assert match["confidence"] == pytest.approx(0.9, abs=1e-6)


@requires_omnigraph
def test_zero_weights_preserve_bm25_order(server, monkeypatch):
    from witan import server as srv

    monkeypatch.setattr(
        srv, "rank_cfg", RankConfig(w_recency=0.0, w_corrob=0.0, w_conf=0.0)
    )
    server.memory_store(kind="pattern", title="t1", content="quux alpha")
    server.memory_store(
        kind="pattern", title="t2", content="quux alpha beta gamma delta"
    )
    raw = srv.client.read("read.gq", "search_all", {"query": "quux alpha"})
    ranked = server.memory_search("quux alpha")
    assert [r["slug"] for r in ranked] == [r["slug"] for r in raw]


@requires_omnigraph
def test_refines_counts_as_corroboration(server):
    from witan import server as srv

    a = server.memory_store(kind="pattern", title="a", content="alpha")
    b = server.memory_store(kind="pattern", title="b", content="beta")
    server.memory_link(a["slug"], b["slug"], "refines")  # a refines b

    # memory_link invalidates the cache, so this rebuilds with the new edge.
    idx = srv._edge_index()
    assert idx["corroboration"][a["slug"]] >= 1
    assert idx["corroboration"][b["slug"]] >= 1


@requires_omnigraph
def test_edge_index_cache_invalidated_on_link(server):
    from witan import server as srv

    a = server.memory_store(kind="pattern", title="a", content="alpha")
    b = server.memory_store(kind="pattern", title="b", content="beta")
    assert srv._edge_index()["superseded"] == set()  # warms the cache
    server.memory_link(a["slug"], b["slug"], "supersedes")
    # invalidation means the just-superseded slug shows up immediately
    assert b["slug"] in srv._edge_index()["superseded"]
