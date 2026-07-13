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
    expected_pair = tuple(sorted((x["slug"], y["slug"])))
    assert expected_pair in pairs

    # include_superseded surfaces the old one again
    with_old = {
        m["slug"]
        for m in server.recall(query="frobnicate widget", include_superseded=True)[
            "memories"
        ]
    }
    assert old["slug"] in with_old


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
def test_recall_expands_tagged_topic_sibling(server):
    a = server.memory_store(kind="pattern", title="seed", content="quokka anchor")
    b = server.memory_store(
        kind="pattern", title="sib", content="nothing matching here"
    )
    server.memory_link(a["slug"], "DATABASE_URL:contract", "tagged")
    server.memory_link(b["slug"], "DATABASE_URL:contract", "tagged")

    out = server.recall(query="quokka anchor", hops=1)
    slugs = {m["slug"] for m in out["memories"]}
    assert a["slug"] in slugs
    assert b["slug"] in slugs  # pulled in via the Tagged edge sibling expansion


@requires_omnigraph
def test_recall_rehydrates_returned_memories(server):
    m = server.memory_store(
        kind="pattern",
        title="seed",
        content="quokka symbol anchor",
        symbol_refs=["python::witan.server.recall"],
    )

    out = server.recall(query="quokka symbol")
    got = next(mem for mem in out["memories"] if mem["slug"] == m["slug"])
    assert got["symbol_refs"] == ["python::witan.server.recall"]


@requires_omnigraph
def test_recall_empty_is_clean(server):
    out = server.recall(query="zzzznomatchquux")
    assert out == {"memories": [], "contradictions": [], "seeds": {}}
