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
    """Zero weights ⇒ the re-rank returns the seed order untouched.

    ★ THE SEED IS SUPPLIED, NOT QUERIED, and that is the whole point of the
    rewrite. This test spent 2026-08-10 → 08-13 red-then-green on unchanged
    ranking code, for two independent reasons, and both are worth knowing:

    1. It compared the WRONG QUERY. It read `search_all` directly, while
       `memory_search` → `_search_rows` resolves a repo first and so runs
       `search_by_repo` (the fixture sets WITAN_REPO). Two different queries;
       the equality never meant anything, even when it passed.
    2. Under omnigraph 0.9.0 the engine returns BM25-TIED ROWS IN A
       NONDETERMINISTIC ORDER — measured directly: identical `search_by_repo`
       calls, same process, same store, same data, came back (t2, t1) six times
       out of six in one store and split 4/2 in the next. 0.8.1 was stable
       across 15 runs, which is why this went red exactly when the 0.9.0 pins
       landed (#216, 2026-08-10). The two fixture documents both contain both
       query terms, so they tie, so the engine is free to order them either way.

    Neither is a fact about the re-rank, which is what this test is named for.
    So the seed is injected: the assertion is now purely "zero weights preserve
    the order they were given", which is deterministic and is the actual claim.
    The engine's tie behaviour is a separate concern — see the set-equality test
    below and tk-upstream-omnigraph-one-common-term-zeroes-an-ent-6489a2.
    """
    from witan import server as srv

    monkeypatch.setattr(
        srv, "rank_cfg", RankConfig(w_recency=0.0, w_corrob=0.0, w_conf=0.0)
    )
    a = server.memory_store(kind="pattern", title="t1", content="quux alpha")
    b = server.memory_store(
        kind="pattern", title="t2", content="quux alpha beta gamma delta"
    )

    # A fixed seed in a deliberately non-alphabetical order, so a re-rank that
    # sorted by anything other than "keep what you were given" would show.
    seed = [
        srv.client.read("read.gq", "get_memory", {"slug": s})[0]
        for s in (b["slug"], a["slug"])
    ]
    monkeypatch.setattr(srv, "_search_rows", lambda *_a, **_kw: list(seed))

    ranked = server.memory_search("quux alpha")

    assert [r["slug"] for r in ranked] == [r["slug"] for r in seed]


@requires_omnigraph
def test_search_returns_the_whole_seed_set_whatever_the_tie_order(server):
    """The end-to-end property that IS stable: nothing is dropped or invented.

    Deliberately a SET comparison. Asserting an order here would re-create the
    flake documented above — with both documents tying on BM25, omnigraph 0.9.0
    orders them arbitrarily, and no amount of client-side sorting can recover an
    order the engine never committed to. witan cannot paper over it either: the
    re-rank derives its relevance proxy FROM the seed position, so imposing a
    tie-break would discard the very signal it exists to preserve.
    """
    a = server.memory_store(kind="pattern", title="s1", content="zonk alpha")
    b = server.memory_store(kind="pattern", title="s2", content="zonk alpha beta")

    hits = server.memory_search("zonk alpha")

    assert {r["slug"] for r in hits} == {a["slug"], b["slug"]}


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
