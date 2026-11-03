"""End-to-end tests for graph-aware recall (spec §8)."""

from .conftest import requires_omnigraph


@requires_omnigraph
def test_recall_without_edges_matches_search(server):
    server.memory_store(kind="pattern", title="a", content="widget alpha")
    server.memory_store(kind="pattern", title="b", content="widget beta")
    search = {h["slug"] for h in server.memory_search("widget")}
    recalled = {m["slug"] for m in server.recall(query="widget")["memories"]}
    assert recalled == search


@requires_omnigraph
def test_recall_by_topic_is_cross_repo(server):
    a = server.memory_store(
        kind="project_fact",
        title="one",
        content="alpha",
        repo="https://github.com/test/one",
        tags=["payments"],
    )
    b = server.memory_store(
        kind="project_fact",
        title="two",
        content="beta",
        repo="https://github.com/test/two",
        tags=["payments"],
    )
    out = server.recall(topic="payments:topic")
    slugs = {m["slug"] for m in out["memories"]}
    assert {a["slug"], b["slug"]} <= slugs


@requires_omnigraph
def test_recall_prunes_superseded_and_flags_contradictions(server):
    old = server.memory_store(
        kind="lesson", title="old", content="frobnicate widget", severity="info"
    )
    new = server.memory_store(
        kind="lesson", title="new", content="frobnicate widget better", severity="info"
    )
    server.memory_link(new["slug"], old["slug"], "supersedes")

    x = server.memory_store(
        kind="lesson", title="x", content="frobnicate widget claim one", severity="info"
    )
    y = server.memory_store(
        kind="lesson", title="y", content="frobnicate widget claim two", severity="info"
    )
    server.memory_link(x["slug"], y["slug"], "contradicts")

    out = server.recall(query="frobnicate widget")
    slugs = {m["slug"] for m in out["memories"]}
    assert old["slug"] not in slugs
    assert {x["slug"], y["slug"]} <= slugs

    pairs = {tuple(sorted((c["a"], c["b"]))) for c in out["contradictions"]}
    assert tuple(sorted((x["slug"], y["slug"]))) in pairs

    # include_superseded surfaces the old one again
    with_old = {
        m["slug"]
        for m in server.recall(query="frobnicate widget", include_superseded=True)[
            "memories"
        ]
    }
    assert old["slug"] in with_old


@requires_omnigraph
def test_recall_finds_a_contradiction_split_across_read_chunks(server, monkeypatch):
    """With one slug per read, the two ends of a pair are always in different
    chunks, so the pair is found only if the target end is filtered locally."""
    x = server.memory_store(
        kind="lesson", title="x", content="quomble gadget claim one", severity="info"
    )
    y = server.memory_store(
        kind="lesson", title="y", content="quomble gadget claim two", severity="info"
    )
    server.memory_link(x["slug"], y["slug"], "contradicts")
    monkeypatch.setattr(server._module, "_SLUG_LIST_READ_BATCH_SIZE", 1)

    out = server.recall(query="quomble gadget")

    assert {x["slug"], y["slug"]} <= {m["slug"] for m in out["memories"]}
    assert {tuple(sorted((c["a"], c["b"]))) for c in out["contradictions"]} == {
        tuple(sorted((x["slug"], y["slug"])))
    }


@requires_omnigraph
def test_recall_expands_related_neighbor(server):
    seed = server.memory_store(
        kind="pattern", title="seed", content="zylph anchor term"
    )
    neighbor = server.memory_store(
        kind="pattern", title="neighbor", content="totally unrelated words"
    )
    server.memory_link(seed["slug"], neighbor["slug"], "related_to")

    out = server.recall(query="zylph anchor", hops=1)
    slugs = [m["slug"] for m in out["memories"]]
    assert seed["slug"] in slugs
    assert neighbor["slug"] in slugs  # pulled in by expansion despite no keyword match
    assert slugs.index(seed["slug"]) < slugs.index(
        neighbor["slug"]
    )  # seed outranks neighbor


@requires_omnigraph
def test_recall_expands_topic_sibling(server):
    a = server.memory_store(
        kind="pattern", title="seed", content="quokka anchor", tags=["shared"]
    )
    b = server.memory_store(
        kind="pattern", title="sib", content="nothing matching here", tags=["shared"]
    )
    out = server.recall(query="quokka anchor", hops=1)
    slugs = {m["slug"] for m in out["memories"]}
    assert a["slug"] in slugs
    assert b["slug"] in slugs  # pulled in via the shared-topic sibling expansion


@requires_omnigraph
def test_recall_empty_is_clean(server):
    out = server.recall(query="zzzznomatchquux")
    assert out == {"memories": [], "contradictions": [], "seeds": {}}


@requires_omnigraph
def test_recall_keeps_a_neighbour_per_source_in_one_read(server):
    """One read covers every source, and a row says which source it came from,
    because a neighbour's cost builds on its own parent's."""
    a = server.memory_store(kind="pattern", title="a", content="alpha")
    b = server.memory_store(kind="pattern", title="b", content="beta")
    c = server.memory_store(kind="pattern", title="c", content="gamma")
    d = server.memory_store(kind="pattern", title="d", content="delta")
    server.memory_link(a["slug"], c["slug"], "related_to")
    server.memory_link(b["slug"], d["slug"], "related_to", confidence="inferred")

    assert server._expand_neighbors([a["slug"], b["slug"]]) == {
        a["slug"]: {c["slug"]: True},
        b["slug"]: {d["slug"]: False},
    }


@requires_omnigraph
def test_recall_reads_do_not_grow_with_seeds_or_candidates(server, monkeypatch):
    """The round trip was the cost: every seed read 6 relations and every
    candidate read itself, so 34 seeds reaching about 900 memories took 1,128
    reads and over three minutes. Reads now grow by chunk of slugs, not by
    memory."""
    tag = ["everywhere"]
    for i in range(3):
        server.memory_store(
            kind="pattern", title=f"s{i}", content=f"zorble seed {i}", tags=tag
        )
    for i in range(9):
        server.memory_store(
            kind="pattern", title=f"n{i}", content=f"unrelated {i}", tags=tag
        )

    reads: list[str] = []
    real_read = server.client.read

    def counting_read(file, query, params=None):
        reads.append(query)
        return real_read(file, query, params)

    monkeypatch.setattr(server.client, "read", counting_read)
    server._invalidate_edge_index()

    out = server.recall(query="zorble", hops=1, limit=5)

    assert len(out["memories"]) == 5
    assert reads.count("memory_score_columns") == 1
    assert reads.count("get_memories_by_slugs") == 1
    assert "get_memory" not in reads
    assert sum(q.startswith("expand_") for q in reads) == 6


@requires_omnigraph
def test_recall_breaks_a_score_tie_by_seed_position(server, monkeypatch):
    """Rows come back in no particular order, so ranking must not depend on it:
    the seed order is the tie-break, as it was when candidates were read one at a
    time. A local store happens to answer in seed order, hence the reversal."""
    for i in range(4):
        server.memory_store(kind="pattern", title=f"t{i}", content="snorkel widget")
    monkeypatch.setattr(server._module, "_score", lambda **_: 0.0)
    real_read = server.client.read

    def reversing_read(file, query, params=None):
        rows = real_read(file, query, params)
        return rows[::-1] if query == "memory_score_columns" else rows

    monkeypatch.setattr(server.client, "read", reversing_read)

    seeded = server.recall(query="snorkel", hops=0)

    assert [m["slug"] for m in seeded["memories"]] == seeded["seeds"]["query"]


@requires_omnigraph
def test_recall_cuts_content_to_the_budget_best_ranked_first(server, monkeypatch):
    monkeypatch.setattr(server._module, "_RESULT_CONTENT_BUDGET_CHARS", 300)
    monkeypatch.setattr(server._module, "_RESULT_EXCERPT_CHARS", 50)
    bodies = [f"plumbus {i} " + "x" * 200 for i in range(3)]
    for i, body in enumerate(bodies):
        server.memory_store(kind="pattern", title=f"m{i}", content=body)

    first, second, third = server.recall(query="plumbus", hops=0)["memories"]

    # Whole while the budget lasts, then what is left of it, then an excerpt.
    assert "content_truncated" not in first
    assert second["content_truncated"] is True
    assert len(second["content"]) == 300 - len(first["content"])
    assert second["content_chars"] == len(bodies[1])
    assert third["content_truncated"] is True
    assert len(third["content"]) == 50
    # The full text is one memory_get away.
    assert server.memory_get(third["slug"])["content"] in bodies


@requires_omnigraph
def test_memory_search_shares_the_content_budget(server, monkeypatch):
    monkeypatch.setattr(server._module, "_RESULT_CONTENT_BUDGET_CHARS", 100)
    monkeypatch.setattr(server._module, "_RESULT_EXCERPT_CHARS", 50)
    for i in range(2):
        server.memory_store(
            kind="pattern", title=f"m{i}", content=f"grommet {i} " + "y" * 200
        )

    hits = server.memory_search("grommet")

    assert all(h["content_truncated"] for h in hits)
    assert [len(h["content"]) for h in hits] == [100, 50]
