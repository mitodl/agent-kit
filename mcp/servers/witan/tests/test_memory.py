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

    facts = server.memory_get_project_facts()
    assert any(f["title"] == "vault" for f in facts)

    patterns = server.memory_list_patterns(language="python")
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
