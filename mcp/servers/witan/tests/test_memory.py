"""End-to-end tests for the memory tools."""

import pytest

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
def test_search_finds_terms_that_appear_only_in_the_title(server):
    """Titles carry the distinguishing nouns, so a term living only there has to
    be findable — `search($m.content, …)` alone could never match it."""
    res = server.memory_store(
        kind="lesson",
        title="zebrafish quokka narwhal",
        content="totally unrelated prose about compaction and fragments",
    )

    hits = server.memory_search("zebrafish quokka narwhal")
    assert [h["slug"] for h in hits] == [res["slug"]]

    # …and through recall, which seeds from the same candidate set.
    recalled = server.recall(query="zebrafish quokka narwhal")
    assert res["slug"] in [m["slug"] for m in recalled["memories"]]


@requires_omnigraph
def test_content_matches_seed_ahead_of_title_only_matches(server):
    """The two BM25 runs aren't on a comparable scale, so content hits are
    appended-to rather than interleaved-with title hits.

    This pins the *seeding* order, not a guarantee about the final result: the
    positional proxy is one weighted term in `_score`, so with recency,
    corroboration or confidence in play a title-only hit can legitimately
    finish higher — as it can among content hits. Both memories here are
    fresh, unlinked and of default confidence, so position is the only signal
    that differs and the seeding order survives to the output.
    """
    title_only = server.memory_store(
        kind="lesson",
        title="quokka narwhal",
        content="prose about compaction and fragments",
    )
    in_content = server.memory_store(
        kind="lesson",
        title="an unremarkable heading",
        content="this body discusses quokka narwhal at length",
    )

    slugs = [h["slug"] for h in server.memory_search("quokka narwhal")]
    assert slugs.index(in_content["slug"]) < slugs.index(title_only["slug"])


@requires_omnigraph
def test_search_returns_a_both_fields_match_exactly_once(server):
    """A row matching on content *and* title is found by both runs; the union
    must dedup it rather than return it twice."""
    both = server.memory_store(
        kind="lesson",
        title="quokka narwhal",
        content="more about the quokka narwhal",
    )

    slugs = [h["slug"] for h in server.memory_search("quokka narwhal")]
    assert slugs.count(both["slug"]) == 1


@requires_omnigraph
def test_title_search_respects_the_kind_filter(server):
    """The `_title` twin of each query carries the same filters — otherwise a
    filtered search would leak title hits from outside the filter."""
    server.memory_store(kind="pattern", title="quokka narwhal", content="unrelated")
    wanted = server.memory_store(
        kind="lesson", title="quokka narwhal too", content="also unrelated"
    )

    hits = server.memory_search("quokka narwhal", kind="lesson")
    assert [h["slug"] for h in hits] == [wanted["slug"]]


@requires_omnigraph
def test_superseded_content_hits_do_not_crowd_out_a_title_match(server, monkeypatch):
    """The result cap has to come after supersession pruning, not before.

    Capping the candidate set first lets content hits that are all about to be
    pruned occupy every slot, discarding the title hits behind them — and the
    search then returns nothing, with a perfectly good title match sitting just
    behind the cut. The cap is patched down so the test needs three memories
    rather than twenty-one; it pins the order of operations, not the number.
    """
    from witan import server as srv

    monkeypatch.setattr(srv, "_SEARCH_LIMIT", 1)

    old = server.memory_store(
        kind="lesson", title="a heading", content="quokka narwhal in the body"
    )
    newer = server.memory_store(
        kind="lesson", title="another heading", content="quokka narwhal again"
    )
    server.memory_link(newer["slug"], old["slug"], "supersedes")
    # `newer` supersedes `old`; now supersede `newer` too, so every content hit
    # is pruned and only the title-only memory should survive.
    replacement = server.memory_store(
        kind="lesson", title="unrelated heading", content="nothing matching here"
    )
    server.memory_link(replacement["slug"], newer["slug"], "supersedes")

    title_only = server.memory_store(
        kind="lesson", title="quokka narwhal", content="body shares no query terms"
    )

    assert [h["slug"] for h in server.memory_search("quokka narwhal")] == [
        title_only["slug"]
    ]


@requires_omnigraph
def test_title_search_on_the_unscoped_query_paths(server):
    """Cover `search_all_title` and `search_by_kind_title`.

    The `server` fixture sets `WITAN_REPO`, so every other test here dispatches
    to the `_by_repo` twins and these two would never run — a typo in one of
    the four hand-duplicated queries would ship silently. `repo=""` is the
    documented way to ask for no repo scope.
    """
    lesson = server.memory_store(
        kind="lesson", title="zebrafish quokka", content="unrelated prose"
    )
    pattern = server.memory_store(
        kind="pattern", title="zebrafish narwhal", content="also unrelated"
    )

    # search_all_title — no repo, no kind.
    found = {h["slug"] for h in server.memory_search("zebrafish", repo="")}
    assert {lesson["slug"], pattern["slug"]} <= found

    # search_by_kind_title — no repo, with kind.
    hits = server.memory_search("zebrafish", repo="", kind="lesson")
    assert [h["slug"] for h in hits] == [lesson["slug"]]


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


# ── memory_update / memory_delete ─────────────────────────────────────


@requires_omnigraph
def test_update_partial_preserves_omitted_fields(server):
    """The read-merge-write trap: update_memory replaces every field it is
    given, so a partial update that skipped the merge would blank the rest."""
    m = server.memory_store(
        kind="lesson",
        title="original title",
        content="original content",
        language="python",
        severity="warning",
        tags=["alpha"],
        confidence=0.7,
    )
    updated = server.memory_update(m["slug"], title="corrected title")

    assert updated["title"] == "corrected title"
    assert updated["content"] == "original content"
    assert updated["language"] == "python"
    assert updated["severity"] == "warning"
    assert updated["tags"] == ["alpha"]
    assert updated["confidence"] == pytest.approx(0.7)  # stored as F32
    assert updated["kind"] == "lesson"


@requires_omnigraph
def test_update_repo_rescopes_and_case_folds(server):
    """The headline case from issue #145 — a memory written against the wrong
    repo. The new key must be canonical or it just mis-scopes differently."""
    m = server.memory_store(kind="pattern", title="misfiled", content="x")
    updated = server.memory_update(m["slug"], repo="https://github.com/MITODL/Other")

    assert updated["repo"] == "https://github.com/mitodl/other"
    assert server.memory_list(kind="pattern", repo="https://github.com/mitodl/other")


@requires_omnigraph
def test_update_tags_dual_writes_topics(server):
    m = server.memory_store(kind="pattern", title="untagged", content="x")
    server.memory_update(m["slug"], tags=["ruff"])

    topics = server.memory_get(m["slug"], include_topics=True)["topics"]
    assert {t["name"] for t in topics} == {"ruff"}


@requires_omnigraph
def test_update_unknown_slug_returns_none(server):
    assert server.memory_update("pat-nope-000000", title="x") is None


@requires_omnigraph
def test_delete_without_confirm_is_a_noop(server):
    m = server.memory_store(kind="pattern", title="keepme", content="x")
    res = server.memory_delete(m["slug"])

    assert res["deleted"] is False
    assert "confirm" in res["reason"]
    assert res["memory"]["title"] == "keepme"
    assert server.memory_get(m["slug"]) is not None


@requires_omnigraph
def test_delete_by_non_author_is_a_noop(server, monkeypatch):
    from witan import server as srv

    m = server.memory_store(kind="pattern", title="someone elses", content="x")
    monkeypatch.setattr(srv, "_current_author", lambda: "not-the-author")

    res = server.memory_delete(m["slug"], confirm=True)

    assert res["deleted"] is False
    assert "only the author" in res["reason"]
    assert server.memory_get(m["slug"]) is not None


@requires_omnigraph
def test_delete_removes_the_node_and_returns_it_for_restore(server):
    m = server.memory_store(
        kind="pattern", title="oops", content="test write", tags=["alpha"]
    )
    res = server.memory_delete(m["slug"], confirm=True)

    assert res["deleted"] is True
    assert res["memory"]["title"] == "oops"
    assert res["memory"]["content"] == "test write"
    assert server.memory_get(m["slug"]) is None


@requires_omnigraph
def test_delete_unknown_slug_is_a_noop(server):
    res = server.memory_delete("pat-nope-000000", confirm=True)
    assert res["deleted"] is False
    assert res["reason"] == "no such memory"


@requires_omnigraph
def test_delete_cascades_incident_edges(server):
    """Deleting a node removes its incident edges in both directions, so no
    dangling Supersedes is left pointing at a slug that no longer exists."""
    old = server.memory_store(kind="pattern", title="old", content="pipenv for venvs")
    new = server.memory_store(kind="pattern", title="new", content="pipenv is retired")
    server.memory_link(new["slug"], old["slug"], "supersedes")
    # `old` is hidden from default search while the Supersedes edge stands.
    assert [h["slug"] for h in server.memory_search("pipenv")] == [new["slug"]]

    server.memory_delete(old["slug"], confirm=True)

    # The Supersedes edge went with the deleted target: nothing is still
    # reported as superseded, and the surviving memory reads normally.
    assert server.memory_get(new["slug"]) is not None
    assert [h["slug"] for h in server.memory_search("pipenv")] == [new["slug"]]
