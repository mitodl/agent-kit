"""Edge properties: writing them on links, reading them back through bound
traversals, and what recall does with the confidence.

The whole feature rests on the BOUND traversal form (``$m $w:relatedto $o``),
which binds the matched edge row so its properties are projectable. That also
drops set semantics — one row per EDGE, not per pair — so the parallel-edge
tests here are not edge cases, they are the contract.
"""

import pytest

from witan.config import RankConfig
from witan.scan import WriteBlocked

from .conftest import requires_omnigraph


def _only(neighbors, kind):
    """The single neighbour of ``kind``, asserting there is exactly one."""
    rows = neighbors["neighbors"][kind]
    assert len(rows) == 1, rows
    return rows[0]


def _topic_edge(server, memory_slug, topic_slug):
    """The edge dict on a memory's link to one Topic."""
    topics = server.memory_get(memory_slug, include_topics=True)["topics"]
    return next(t["edge"] for t in topics if t["slug"] == topic_slug)


# ── Writing ───────────────────────────────────────────────────────


@requires_omnigraph
def test_link_stamps_provenance_on_the_edge(server):
    a = server.memory_store(kind="pattern", title="a", content="alpha content")
    b = server.memory_store(kind="pattern", title="b", content="beta content")

    res = server.memory_link(
        a["slug"], b["slug"], "related_to", role="both configure the sidecar"
    )
    assert res["linked"] is True
    assert res["edge"]["confidence"] == "asserted"
    # `role` is deliberately absent from the response — see
    # test_link_does_not_report_the_role_it_was_given. Read it back instead.

    edge = _only(server.memory_neighbors(a["slug"]), "related_to")["edge"]
    assert edge["confidence"] == "asserted"
    assert edge["role"] == "both configure the sidecar"
    assert edge["author"]
    assert edge["created_at"]


@requires_omnigraph
def test_edge_timestamp_is_the_links_not_the_nodes(server):
    """The point of ``created_at`` on the edge: when the LINK was made is not
    derivable from either endpoint, and a link is usually newer than both."""
    a = server.memory_store(kind="pattern", title="a", content="alpha content")
    b = server.memory_store(kind="pattern", title="b", content="beta content")
    server.memory_link(a["slug"], b["slug"], "related_to")

    row = _only(server.memory_neighbors(a["slug"]), "related_to")
    assert row["created_at"] == server.memory_get(b["slug"])["created_at"]
    assert row["edge"]["created_at"] >= row["created_at"]


@requires_omnigraph
def test_role_is_optional(server):
    a = server.memory_store(kind="pattern", title="a", content="alpha content")
    b = server.memory_store(kind="pattern", title="b", content="beta content")
    server.memory_link(a["slug"], b["slug"], "refines")

    edge = _only(server.memory_neighbors(a["slug"]), "refines")["edge"]
    assert edge["role"] is None
    assert edge["confidence"] == "asserted"


@requires_omnigraph
def test_caller_can_mark_a_link_inferred(server):
    a = server.memory_store(kind="pattern", title="a", content="alpha content")
    b = server.memory_store(kind="pattern", title="b", content="beta content")
    server.memory_link(a["slug"], b["slug"], "related_to", confidence="inferred")

    edge = _only(server.memory_neighbors(a["slug"]), "related_to")["edge"]
    assert edge["confidence"] == "inferred"


@requires_omnigraph
@pytest.mark.parametrize(
    "kind", ["supersedes", "refines", "applies_to", "contradicts", "related_to"]
)
def test_every_memory_edge_kind_carries_properties(server, kind):
    a = server.memory_store(kind="pattern", title="a", content="alpha content")
    b = server.memory_store(kind="pattern", title="b", content="beta content")
    server.memory_link(a["slug"], b["slug"], kind, role="why")

    edge = _only(server.memory_neighbors(a["slug"]), kind)["edge"]
    assert edge["role"] == "why"
    assert edge["confidence"] == "asserted"


# ── Tagged: the one edge witan writes both ways ───────────────────


@requires_omnigraph
def test_tag_derived_edges_are_inferred(server):
    """A Topic promoted from a free-string tag is witan's guess, not a claim
    anyone made — so the Tagged edge behind it is `inferred`."""
    m = server.memory_store(
        kind="pattern", title="a", content="alpha content", tags=["vault"]
    )
    topic = server.topic_get("vault:topic")
    assert [t["slug"] for t in topic["memories"]] == [m["slug"]]
    edge = _topic_edge(server, m["slug"], topic["topic"]["slug"])
    assert edge["confidence"] == "inferred"
    assert edge["role"] == "tag"


@requires_omnigraph
def test_explicit_tagged_link_is_asserted(server):
    m = server.memory_store(kind="pattern", title="a", content="alpha content")
    res = server.memory_link(m["slug"], "DATABASE_URL:contract", "tagged")
    assert res["edge"]["confidence"] == "asserted"
    assert _topic_edge(server, m["slug"], res["to"])["confidence"] == "asserted"


# ── Parallel edges ────────────────────────────────────────────────


@requires_omnigraph
def test_relinking_does_not_duplicate_a_neighbour(server):
    """An insert is not an upsert — a second link writes a PARALLEL edge, which
    the bound traversal reports as a second row. memory_neighbors must still
    report one entry per neighbour, or its contract changes under every caller
    that ever re-linked a pair."""
    a = server.memory_store(kind="pattern", title="a", content="alpha content")
    b = server.memory_store(kind="pattern", title="b", content="beta content")
    server.memory_link(a["slug"], b["slug"], "related_to", role="first")
    server.memory_link(a["slug"], b["slug"], "related_to", role="second")

    assert (
        _only(server.memory_neighbors(a["slug"]), "related_to")["edge"]["role"]
        == "second"
    )


@requires_omnigraph
def test_symmetric_union_still_dedupes_across_directions(server):
    """A pair linked in both directions is one neighbour, not two — the union
    of the out- and in-direction queries has always collapsed here."""
    a = server.memory_store(kind="pattern", title="a", content="alpha content")
    b = server.memory_store(kind="pattern", title="b", content="beta content")
    server.memory_link(a["slug"], b["slug"], "contradicts")
    server.memory_link(b["slug"], a["slug"], "contradicts")

    assert _only(server.memory_neighbors(a["slug"]), "contradicts")["slug"] == b["slug"]


@requires_omnigraph
def test_newest_edge_wins_across_the_symmetric_union(server):
    """Each direction's query is sorted independently, so read.gq's `order`
    alone cannot decide newest-wins across the union: taking the first row per
    slug surfaces the older OUT edge over a newer IN edge."""
    a = server.memory_store(kind="pattern", title="a", content="alpha content")
    b = server.memory_store(kind="pattern", title="b", content="beta content")
    server.memory_link(a["slug"], b["slug"], "related_to", role="out-older")
    server.memory_link(b["slug"], a["slug"], "related_to", role="in-newer")

    row = _only(server.memory_neighbors(a["slug"]), "related_to")
    assert row["edge"]["role"] == "in-newer"


@requires_omnigraph
def test_an_unstamped_edge_never_beats_a_stamped_one(server):
    """A null `created_at` means "written before provenance existed", not
    "written at the epoch" — so it must lose to any stamped edge, whichever
    direction it arrives from."""
    a = server.memory_store(kind="pattern", title="a", content="alpha content")
    b = server.memory_store(kind="pattern", title="b", content="beta content")
    server.client.change(
        "mutations.gq",
        "link_related_to",
        {
            "from": b["slug"],
            "to": a["slug"],
            "confidence": None,
            "role": None,
            "author": None,
            "created_at": None,
        },
    )
    server.memory_link(a["slug"], b["slug"], "related_to", role="stamped")

    edge = _only(server.memory_neighbors(a["slug"]), "related_to")["edge"]
    assert edge["role"] == "stamped"


# ── The response must not outrun what was stored ──────────────────


@requires_omnigraph
def test_link_does_not_report_the_role_it_was_given(server):
    """The write guard can redact `role` on the way in and returns a rewritten
    params dict rather than mutating ours, so echoing the caller's `role` back
    would state a value that is not in the graph — and re-emit the very text
    that was redacted. The machine-set properties are never scanned."""
    a = server.memory_store(kind="pattern", title="a", content="alpha content")
    b = server.memory_store(kind="pattern", title="b", content="beta content")

    res = server.memory_link(a["slug"], b["slug"], "related_to", role="why these two")
    assert set(res["edge"]) == {"confidence", "author", "created_at"}
    # …and the stored role is still readable, from the graph.
    edge = _only(server.memory_neighbors(a["slug"]), "related_to")["edge"]
    assert edge["role"] == "why these two"


# ── Parallel Tagged edges ─────────────────────────────────────────


@requires_omnigraph
def test_updating_a_memory_does_not_relink_tags_it_already_has(server):
    """An insert is not an upsert, so re-linking an existing tag wrote a second
    parallel Tagged edge — three updates left four edges to one Topic.
    Invisible under set semantics; quadratic once `topic_siblings` binds."""
    m = server.memory_store(
        kind="pattern", title="a", content="alpha content", tags=["shared"]
    )
    for i in range(3):
        server.memory_update(m["slug"], content=f"alpha {i}", tags=["shared"])

    topic = server.topic_get("shared:topic")["topic"]["slug"]
    edges = server.client.read("read.gq", "topics_for_memory", {"slug": m["slug"]})
    assert [e["slug"] for e in edges] == [topic]


@requires_omnigraph
def test_updating_a_memory_still_links_a_newly_added_tag(server):
    """Skipping already-linked topics must not skip the new ones."""
    m = server.memory_store(
        kind="pattern", title="a", content="alpha content", tags=["shared"]
    )
    server.memory_update(m["slug"], tags=["shared", "added"])

    node = server.memory_get(m["slug"], include_topics=True)
    assert {t["name"] for t in node["topics"]} == {"shared", "added"}


@requires_omnigraph
def test_topic_siblings_does_not_multiply_by_parallel_edges(server):
    """Only the sibling's Tagged edge is bound. Binding the seed's as well made
    this the cross product of both sides' parallel edges — 32 rows for two
    memories carrying four edges each, against 2 for one link apiece."""
    a = server.memory_store(
        kind="pattern", title="a", content="alpha content", tags=["shared"]
    )
    b = server.memory_store(
        kind="pattern", title="b", content="beta content", tags=["shared"]
    )
    topic = server.topic_get("shared:topic")["topic"]["slug"]
    # Force the parallel edges an older witan accumulated, on BOTH sides.
    for slug in (a["slug"], b["slug"]):
        for _ in range(3):
            server.client.change(
                "mutations.gq",
                "link_tagged",
                {
                    "from": slug,
                    "to": topic,
                    "confidence": "inferred",
                    "role": "tag",
                    "author": "t",
                    "created_at": "2026-09-01T00:00:00Z",
                },
            )

    rows = server.client.read("read.gq", "topic_siblings", {"slug": a["slug"]})
    # 4 edges on the sibling side; the seed side collapses by set semantics.
    assert len(rows) == 8
    assert server._expand_neighbors(a["slug"]) == {b["slug"]: False}


# ── Confidence-weighted recall ────────────────────────────────────


@requires_omnigraph
def test_inferred_neighbour_ranks_below_an_asserted_one(server):
    """Two neighbours, same hop, neither matching the query — the one reached by
    an asserted link outranks the one reached by an inferred link."""
    seed = server.memory_store(
        kind="pattern", title="seed", content="zylph anchor term"
    )
    asserted = server.memory_store(
        kind="pattern", title="asserted", content="unrelated words one"
    )
    inferred = server.memory_store(
        kind="pattern", title="inferred", content="unrelated words two"
    )
    server.memory_link(seed["slug"], asserted["slug"], "related_to")
    server.memory_link(
        seed["slug"], inferred["slug"], "related_to", confidence="inferred"
    )

    slugs = [m["slug"] for m in server.recall(query="zylph anchor", hops=1)["memories"]]
    assert slugs.index(asserted["slug"]) < slugs.index(inferred["slug"])


@requires_omnigraph
def test_inferred_neighbour_is_still_returned(server):
    """The surcharge reranks; it never prunes. An inferred neighbour that used
    to be recalled must still be recalled."""
    seed = server.memory_store(
        kind="pattern", title="seed", content="quokka anchor", tags=["shared"]
    )
    sib = server.memory_store(
        kind="pattern", title="sib", content="nothing matching here", tags=["shared"]
    )
    slugs = {
        m["slug"] for m in server.recall(query="quokka anchor", hops=1)["memories"]
    }
    assert {seed["slug"], sib["slug"]} <= slugs


@requires_omnigraph
def test_zero_weight_restores_uniform_hops(server, monkeypatch):
    """The knob is an off switch, not just a dial — at 0 an inferred hop costs
    exactly what an asserted one costs, which is the pre-feature behaviour."""
    seed = server.memory_store(
        kind="pattern", title="seed", content="zylph anchor term"
    )
    inferred = server.memory_store(
        kind="pattern", title="inferred", content="unrelated words two"
    )
    server.memory_link(
        seed["slug"], inferred["slug"], "related_to", confidence="inferred"
    )

    monkeypatch.setattr(
        server._module, "rank_cfg", RankConfig(w_inferred_edge=0.0), raising=True
    )
    assert server._expand_neighbors(seed["slug"]) == {inferred["slug"]: False}


@requires_omnigraph
def test_the_surcharge_accumulates_along_the_path(server):
    """Cost is the parent's cost plus a hop plus this edge's surcharge. Charging
    from the hop NUMBER instead let an inferred first edge launder itself by
    being extended: its neighbour-of-a-neighbour landed at exactly 2.0, the
    same as a route that was asserted the whole way."""
    seed = server.memory_store(kind="pattern", title="seed", content="zylph anchor")
    guessed = server.memory_store(kind="pattern", title="mid", content="unrelated one")
    beyond = server.memory_store(kind="pattern", title="far", content="unrelated two")
    server.memory_link(
        seed["slug"], guessed["slug"], "related_to", confidence="inferred"
    )
    server.memory_link(guessed["slug"], beyond["slug"], "related_to")

    cost = {seed["slug"]: 0.0}
    server._expand_from_seeds(cost, 2)
    assert cost[guessed["slug"]] == pytest.approx(1.35)
    # Asserted second hop, but the first was a guess — the penalty survives it.
    assert cost[beyond["slug"]] == pytest.approx(2.35)


@requires_omnigraph
def test_best_route_wins_when_a_neighbour_is_reachable_both_ways(server):
    """One asserted route is enough. An extra inferred route to a memory we
    already trust is corroboration, not a reason to trust it less."""
    seed = server.memory_store(
        kind="pattern", title="seed", content="zylph anchor", tags=["shared"]
    )
    both = server.memory_store(
        kind="pattern", title="both", content="unrelated words", tags=["shared"]
    )
    # The shared tag alone is an inferred route...
    assert server._expand_neighbors(seed["slug"])[both["slug"]] is False
    # ...and naming the link is enough to make the pair asserted.
    server.memory_link(seed["slug"], both["slug"], "related_to")
    assert server._expand_neighbors(seed["slug"])[both["slug"]] is True


@requires_omnigraph
def test_unstamped_edges_score_as_asserted(server):
    """Every edge written before this feature has null properties and nothing
    backfills them. Reading null as `inferred` would silently rerank the whole
    existing graph, so null keeps the weight it had."""
    a = server.memory_store(kind="pattern", title="a", content="alpha content")
    b = server.memory_store(kind="pattern", title="b", content="beta content")
    server.client.change(
        "mutations.gq",
        "link_related_to",
        {
            "from": a["slug"],
            "to": b["slug"],
            "confidence": None,
            "role": None,
            "author": None,
            "created_at": None,
        },
    )
    assert server._expand_neighbors(a["slug"]) == {b["slug"]: True}
    assert _only(server.memory_neighbors(a["slug"]), "related_to")["edge"] == {
        "confidence": None,
        "role": None,
        "author": None,
        "created_at": None,
    }


# ── Schema drift ──────────────────────────────────────────────────


@requires_omnigraph
def test_missing_edge_properties_name_the_fix(server):
    """A deployed graph can lag the release that added these properties. The
    engine's own message ("has no property") does not say what to do about it;
    this is what turns it into a one-command fix rather than a support ticket."""
    engine_error = RuntimeError(
        "type error: T6: edge `RelatedTo` has no property `confidence`"
    )
    with pytest.raises(RuntimeError) as excinfo:
        with server._module._edge_property_errors():
            raise engine_error
    assert "witan migrate schema" in str(excinfo.value)
    assert excinfo.value.__cause__ is engine_error


@requires_omnigraph
def test_unrelated_errors_pass_through_untouched(server):
    boom = RuntimeError("connection refused")
    with pytest.raises(RuntimeError, match="connection refused") as excinfo:
        with server._module._edge_property_errors():
            raise boom
    assert excinfo.value is boom


@requires_omnigraph
def test_a_write_refusal_keeps_its_type(server):
    """`WriteBlocked` is a RuntimeError subclass, and the guard sits on the same
    write path. Rewriting a refusal into a plain RuntimeError would lose the
    type every caller distinguishes a policy block by."""
    refusal = WriteBlocked("link_related_to", [])
    with pytest.raises(WriteBlocked) as excinfo:
        with server._module._edge_property_errors():
            raise refusal
    assert excinfo.value is refusal
