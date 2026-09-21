"""Tests for the composite memory re-rank (spec §7).

The scoring math is unit-tested directly via ``_score`` (equal norm_bm25, vary
one term), because varying one term end-to-end would mean writing documents
that differ in exactly one way BM25 can see. Plumbing, the relevance
normalisation and the order-preserving degenerate case are tested through the
public tools and through ``_with_relevance``.
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
    #
    # The seed goes through `_with_relevance` rather than being hand-stamped,
    # because that is what `_search_rows` does to a real run and the two must
    # not be able to drift. `score` descends with the seed order so the rows
    # arrive exactly as the engine would deliver them.
    seed = srv._with_relevance(
        [
            srv.client.read("read.gq", "get_memory", {"slug": s})[0] | {"score": score}
            for s, score in ((b["slug"], 2.0), (a["slug"], 1.0))
        ],
        srv._CONTENT_BAND,
    )
    monkeypatch.setattr(srv, "_search_rows", lambda *_a, **_kw: list(seed))

    ranked = server.memory_search("quux alpha")

    assert [r["slug"] for r in ranked] == [r["slug"] for r in seed]
    # And the scoring keys do not leak into what the caller receives.
    assert not any("score" in r or "_relevance" in r for r in ranked)


# ── relevance normalisation (omnigraph 0.11 `bm25(...) as score`) ──


def test_relevance_keeps_the_engine_s_spacing_within_a_run(server):
    """Rank position said how hits ORDER; it could not say how far apart they
    are, so for any three rows it reported 1.0/0.5/0.0 and wrote the last one
    off entirely. These are the scores the 0.11.0 binary returned for a
    three-document corpus, scaled into the content band."""
    from witan import server as srv

    rows = srv._with_relevance(
        [{"score": 0.675232}, {"score": 0.3818718}, {"score": 0.23144846}],
        srv._CONTENT_BAND,
    )
    got = [r["_relevance"] for r in rows]

    assert got[0] == 1.0
    assert got[1] == pytest.approx(0.5 + 0.5 * 0.5655, abs=1e-3)
    assert got[2] == pytest.approx(0.5 + 0.5 * 0.3428, abs=1e-3)
    # The weakest match is no longer written off at the band's floor.
    assert got[2] > srv._CONTENT_BAND[0]


def test_every_content_hit_outranks_every_title_only_hit(server):
    """★ THE POLICY THE BANDS EXIST FOR, and it is not hypothetical: without
    them this is what regresses.

    Normalising each run against its own best puts the top title-only hit at
    1.0 — level with the top content hit. Measured on 0.11.0, a memory whose
    title matched and whose content said nothing relevant scored 0.902 on
    `title` against 0.675 for the best `content` match, so the naive version
    moves it by the whole `w_bm25` term (1.0), more than recency can ever
    contribute (0.3). Before 0.11 the concatenated runs made this true by
    construction; the bands are what keeps it true now it has to be chosen.
    """
    from witan import server as srv

    content = srv._with_relevance(
        [{"score": 0.675232}, {"score": 0.3818718}], srv._CONTENT_BAND
    )
    title = srv._with_relevance([{"score": 0.90204775}], srv._TITLE_BAND)

    assert min(r["_relevance"] for r in content) > max(r["_relevance"] for r in title)
    # The title hit outscored every content hit on the raw number.
    assert 0.90204775 > max(0.675232, 0.3818718)


def test_tied_scores_get_equal_relevance(server):
    """Rank position spread tied rows across the whole range because they
    happened to arrive in some order. Equal scores now mean equal relevance."""
    from witan import server as srv

    rows = srv._with_relevance([{"score": 1.5}, {"score": 1.5}], srv._CONTENT_BAND)

    assert [r["_relevance"] for r in rows] == [1.0, 1.0]


def test_an_empty_run_normalises_to_nothing(server):
    from witan import server as srv

    assert srv._with_relevance([], srv._TITLE_BAND) == []


def test_an_all_zero_run_does_not_divide_by_zero(server):
    """Cannot arise for rows `search()` matched, but the guard must hold: every
    row is equally uninformative, so they all take the band's top."""
    from witan import server as srv

    rows = srv._with_relevance([{"score": 0.0}, {"score": 0.0}], srv._TITLE_BAND)

    assert [r["_relevance"] for r in rows] == [0.5, 0.5]


def test_a_missing_score_is_not_fatal_to_a_search(server):
    """0.11 omits null fields from a row and `read` restores them as None. A
    null score should never happen for a matched row, but ranking it at the
    band's floor beats raising out of a search the caller asked for."""
    from witan import server as srv

    rows = srv._with_relevance([{"score": 2.0}, {"score": None}], srv._CONTENT_BAND)

    assert [r["_relevance"] for r in rows] == [1.0, 0.5]


@requires_omnigraph
def test_the_relevance_reaching_the_re_rank_comes_from_the_engine(server):
    """End-to-end: the whole chain, from the projected score in read.gq through
    `_search_rows`, produces something the rank-position proxy could not.

    Three documents matching one term at very different strengths. The proxy
    was a function of the row COUNT alone, so for any three rows it said
    1.0/0.5/0.0 — the weakest match written off entirely. A real score cannot
    do that: the third document does match, so its relevance is above zero.
    """
    from witan import server as srv

    term = "flibbertigibbet"
    server.memory_store(
        kind="pattern", title="h1", content=f"{term} {term} {term} {term}"
    )
    server.memory_store(
        kind="pattern", title="h2", content=f"{term} among a few other words here"
    )
    server.memory_store(
        kind="pattern",
        title="h3",
        content=f"padding padding padding padding padding {term} " + "padding " * 30,
    )

    rows = srv._search_rows(term, None, None)
    relevance = [r["_relevance"] for r in rows]

    assert len(relevance) == 3
    # The assertion rank position could never satisfy: three content rows sat
    # at exactly 1.0/0.5/0.0 whatever they scored, so the weakest was written
    # off. Real spacing puts all three strictly inside the content band.
    assert relevance[0] == 1.0
    assert all(r > srv._CONTENT_BAND[0] for r in relevance)
    assert relevance[1] != pytest.approx(0.5)
    assert relevance == sorted(relevance, reverse=True)


@requires_omnigraph
def test_search_returns_the_whole_seed_set_whatever_the_tie_order(server):
    """The end-to-end property that IS stable: nothing is dropped or invented.

    Deliberately a SET comparison. Asserting an order here would re-create the
    flake documented above — with both documents tying on BM25, omnigraph 0.9.0
    orders them arbitrarily, and no amount of client-side sorting can recover an
    order the engine never committed to.

    Now that the engine projects the score (0.11), tied rows arrive with EQUAL
    relevance rather than with the 1.0-and-0.0 the old rank-position proxy
    invented for them, so the re-rank no longer manufactures a difference the
    engine did not report. It still cannot invent an order, which is why this
    stays a set comparison.
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
