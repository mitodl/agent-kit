"""End-to-end tests for the memory tools."""

from .conftest import requires_omnigraph


@requires_omnigraph
def test_store_and_get(server):
    res = server.memory_store(
        kind="pattern",
        title="uv usage",
        content="always use uv for python venvs",
        language="python",
    )
    assert res["slug"].startswith("pat-")
    assert res["repo"] == "https://github.com/test/repo"

    node = server.memory_get(res["slug"])
    assert node["title"] == "uv usage"
    assert node["language"] == "python"


@requires_omnigraph
def test_store_bare_string_tags_and_symbol_refs_are_coerced(server):
    """A single string (not wrapped in a list) is a common LLM-caller mistake —
    iterating it char-by-char would create one-letter Topic nodes."""
    res = server.memory_store(
        kind="pattern",
        title="stringy inputs",
        content="content",
        tags="alpha",
        symbol_refs="repo#path::Name",
    )
    node = server.memory_get(res["slug"])
    assert node["tags"] == ["alpha"]
    assert node["symbol_refs"] == ["repo#path::Name"]


@requires_omnigraph
def test_search_bm25_ranked(server):
    server.memory_store(
        kind="pattern",
        title="uv usage",
        content="always use uv for python virtual environments",
    )
    server.memory_store(
        kind="lesson",
        title="no raw sql",
        content="avoid raw sql in django views",
        severity="warning",
    )

    hits = server.memory_search("uv virtual environments")
    assert hits and hits[0]["title"] == "uv usage"


@requires_omnigraph
def test_search_kind_filter(server):
    server.memory_store(kind="pattern", title="caching", content="cache sql results")
    server.memory_store(kind="lesson", title="sql danger", content="raw sql is risky")

    hits = server.memory_search("sql", kind="lesson")
    assert hits and all(h["kind"] == "lesson" for h in hits)


@requires_omnigraph
def test_project_facts_and_patterns(server):
    server.memory_store(
        kind="project_fact",
        title="vault",
        content="secrets via vault",
        category="deployment",
    )
    server.memory_store(
        kind="pattern", title="ruff", content="lint with ruff", language="python"
    )

    facts = server.memory_list(kind="project_fact")
    assert any(f["title"] == "vault" for f in facts)

    patterns = server.memory_list(kind="pattern", language="python")
    assert any(p["title"] == "ruff" for p in patterns)
    # the project_fact must not appear among patterns
    assert all(p["title"] != "vault" for p in patterns)


@requires_omnigraph
def test_memory_list_filters_by_kind(server):
    server.memory_store(kind="pattern", title="p", content="a pattern")
    server.memory_store(kind="lesson", title="l", content="a lesson", severity="info")
    server.memory_store(kind="project_fact", title="f", content="a fact")

    all_kinds = {m["kind"] for m in server.memory_list()}
    assert {"pattern", "lesson", "project_fact"} <= all_kinds

    # --kind returns only that kind (not just project_fact)
    assert [m["kind"] for m in server.memory_list(kind="lesson")] == ["lesson"]
    assert [m["kind"] for m in server.memory_list(kind="pattern")] == ["pattern"]
    assert server.memory_list(kind="agent_context") == []


@requires_omnigraph
def test_supersedes_hides_old_from_search(server):
    old = server.memory_store(
        kind="pattern", title="old uv note", content="use uv for python venvs"
    )
    new = server.memory_store(
        kind="pattern", title="new uv note", content="use uv for python venvs today"
    )
    server.memory_link(new["slug"], old["slug"], "supersedes")

    slugs = {h["slug"] for h in server.memory_search("uv python venvs")}
    assert old["slug"] not in slugs
    assert new["slug"] in slugs

    with_old = {
        h["slug"]
        for h in server.memory_search("uv python venvs", include_superseded=True)
    }
    assert old["slug"] in with_old


@requires_omnigraph
def test_related_to_is_symmetric(server):
    a = server.memory_store(kind="pattern", title="a", content="alpha content")
    b = server.memory_store(kind="pattern", title="b", content="beta content")
    server.memory_link(a["slug"], b["slug"], "related_to")

    # stored a -> b, but b's neighbours union both directions
    nb = server.memory_neighbors(b["slug"])
    related = {n["slug"] for n in nb["neighbors"]["related_to"]}
    assert a["slug"] in related


@requires_omnigraph
def test_contradicts_not_hidden(server):
    a = server.memory_store(kind="lesson", title="x", content="contradiction topic one")
    b = server.memory_store(kind="lesson", title="y", content="contradiction topic two")
    server.memory_link(a["slug"], b["slug"], "contradicts")

    slugs = {h["slug"] for h in server.memory_search("contradiction topic")}
    assert {a["slug"], b["slug"]} <= slugs

    nb = server.memory_neighbors(a["slug"])
    assert b["slug"] in {n["slug"] for n in nb["neighbors"]["contradicts"]}


@requires_omnigraph
def test_link_missing_slug_is_noop(server):
    a = server.memory_store(kind="pattern", title="real", content="a real memory")
    res = server.memory_link(a["slug"], "pat-does-not-exist", "related_to")
    assert res["linked"] is False
    assert "pat-does-not-exist" in res["missing"]
    # no dead edge surfaces in a typed read
    assert server.memory_neighbors(a["slug"])["neighbors"]["related_to"] == []


@requires_omnigraph
def test_link_self_is_rejected(server):
    a = server.memory_store(kind="pattern", title="solo", content="a lone memory")
    res = server.memory_link(a["slug"], a["slug"], "supersedes")
    assert res["linked"] is False
    assert res["reason"] == "cannot link a memory to itself"
    # no self-loop written
    assert server.memory_neighbors(a["slug"])["neighbors"]["supersedes"] == []


@requires_omnigraph
def test_neighbors_kinds_subset_and_empty(server):
    a = server.memory_store(kind="pattern", title="a", content="alpha")
    b = server.memory_store(kind="pattern", title="b", content="beta")
    server.memory_link(a["slug"], b["slug"], "related_to")

    # explicit subset returns only the requested kind
    only_related = server.memory_neighbors(a["slug"], kinds=["related_to"])
    assert list(only_related["neighbors"]) == ["related_to"]

    # explicit empty list returns no kinds (not "all kinds")
    assert server.memory_neighbors(a["slug"], kinds=[])["neighbors"] == {}

    # omitting kinds (None) returns all kinds
    assert set(server.memory_neighbors(a["slug"])["neighbors"]) == {
        "supersedes",
        "refines",
        "applies_to",
        "contradicts",
        "related_to",
    }


@requires_omnigraph
def test_tags_create_and_reuse_topics(server):
    a = server.memory_store(kind="pattern", title="a", content="content a", tags=["uv"])
    b = server.memory_store(kind="pattern", title="b", content="content b", tags=["uv"])

    got = server.topic_get("tp-topic-uv")
    assert got["topic"]["name"] == "uv"
    assert got["topic"]["kind"] == "topic"
    slugs = {m["slug"] for m in got["memories"]}
    assert {a["slug"], b["slug"]} <= slugs

    # the second store re-used the node — no duplicate
    by_name = server.topic_get("uv:topic")
    assert by_name["topic"]["slug"] == "tp-topic-uv"


@requires_omnigraph
def test_memories_for_topic_cross_repo(server):
    a = server.memory_store(
        kind="project_fact",
        title="repo one fact",
        content="alpha",
        repo="https://github.com/test/one",
        tags=["cryptography"],
    )
    b = server.memory_store(
        kind="project_fact",
        title="repo two fact",
        content="beta",
        repo="https://github.com/test/two",
        tags=["cryptography"],
    )
    got = server.topic_get("tp-topic-cryptography")
    slugs = {m["slug"] for m in got["memories"]}
    assert {a["slug"], b["slug"]} <= slugs


@requires_omnigraph
def test_memory_link_tagged_autocreates_topic(server):
    a = server.memory_store(
        kind="lesson", title="x", content="lesson x", severity="info"
    )
    res = server.memory_link(a["slug"], "DATABASE_URL:contract", "tagged")
    assert res["linked"] is True
    assert res["to"] == "tp-contract-database-url"

    node = server.memory_get(a["slug"], include_topics=True)
    assert any(t["kind"] == "contract" for t in node["topics"])


@requires_omnigraph
def test_migrate_topics_is_idempotent_after_dual_write(server):
    # memory_store already dual-writes tags → topics, so a migration over the same
    # data is a no-op: it must not create duplicate topics or Tagged edges.
    server.memory_store(kind="pattern", title="legacy", content="old", tags=["ruff"])
    server.migrate_topics()
    second = server.migrate_topics()
    assert second["edges_created"] == 0
    assert second["topics_created"] == 0
    got = server.topic_get("tp-topic-ruff")
    assert got is not None and got["memories"]


@requires_omnigraph
def test_topic_get_resolves_name_kind_case_insensitively(server):
    # Stored tag keeps its casing in the node name, but the slug is normalised.
    m = server.memory_store(kind="pattern", title="cased", content="x", tags=["UV"])
    # Querying with different casing still resolves to tp-topic-uv.
    got = server.topic_get("uv:topic")
    assert got is not None
    assert got["topic"]["slug"] == "tp-topic-uv"
    assert m["slug"] in {x["slug"] for x in got["memories"]}


@requires_omnigraph
def test_blank_tags_are_skipped(server):
    m = server.memory_store(
        kind="pattern", title="blanky", content="x", tags=["uv", "   ", ""]
    )
    topics = server.memory_get(m["slug"], include_topics=True)["topics"]
    assert {t["name"] for t in topics} == {"uv"}


@requires_omnigraph
def test_non_latin_tag_gets_distinct_slug(server):
    # Names with no [a-z0-9] must not all collapse to tp-topic-.
    a = server.memory_store(kind="pattern", title="ja", content="x", tags=["日本語"])
    b = server.memory_store(kind="pattern", title="ko", content="y", tags=["한국어"])
    ta = server.memory_get(a["slug"], include_topics=True)["topics"][0]["slug"]
    tb = server.memory_get(b["slug"], include_topics=True)["topics"][0]["slug"]
    assert ta != tb
    assert ta != "tp-topic-" and tb != "tp-topic-"
