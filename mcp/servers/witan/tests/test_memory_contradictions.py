"""``memory_contradictions``: every Contradicts pair, unseeded and unranked."""

from .conftest import requires_omnigraph

HERE = "https://github.com/test/repo"
ELSEWHERE = "https://github.com/test/other"


def _memory(server, title, repo=HERE):
    return server.memory_store(
        kind="pattern", title=title, content=f"{title} content", repo=repo
    )["slug"]


def _pairs(rows):
    return [{row["a"]["slug"], row["b"]["slug"]} for row in rows]


@requires_omnigraph
def test_empty_graph_is_an_empty_list(server):
    assert server.memory_contradictions(repo="") == []


@requires_omnigraph
def test_reports_both_endpoints_and_the_edge(server):
    a = _memory(server, "alpha")
    b = _memory(server, "beta")
    server.memory_link(a, b, "contradicts", role="disagree on the default")

    [row] = server.memory_contradictions()

    assert row["a"]["slug"] == a
    assert row["b"]["slug"] == b
    assert row["a"]["title"] == "alpha"
    assert row["b"]["title"] == "beta"
    assert row["a"]["content"] == "alpha content"
    assert row["b"]["content"] == "beta content"
    for side in (row["a"], row["b"]):
        assert set(side) == {
            "slug",
            "title",
            "kind",
            "repo",
            "author",
            "updated_at",
            "content",
            "confidence",
        }
        assert side["kind"] == "pattern"
        assert side["repo"] == HERE
    assert row["edge"]["role"] == "disagree on the default"
    assert row["edge"]["confidence"] == "asserted"
    assert row["edge"]["author"]
    assert row["edge"]["created_at"]


@requires_omnigraph
def test_other_edge_kinds_are_not_contradictions(server):
    a = _memory(server, "alpha")
    b = _memory(server, "beta")
    server.memory_link(a, b, "related_to")

    assert server.memory_contradictions(repo="") == []


@requires_omnigraph
def test_a_pair_stored_either_way_appears_once(server):
    a = _memory(server, "alpha")
    b = _memory(server, "beta")
    c = _memory(server, "gamma")
    server.memory_link(a, b, "contradicts")
    server.memory_link(c, a, "contradicts")

    assert sorted(_pairs(server.memory_contradictions()), key=sorted) == sorted(
        [{a, b}, {a, c}], key=sorted
    )


@requires_omnigraph
def test_a_pair_stored_both_ways_is_one_row_carrying_the_newer_edge(server):
    """Both directions are distinct edges under @key(@src, @dst). The row takes
    the newer one's metadata, including its direction, whichever direction the
    newer one is."""
    a = _memory(server, "alpha")
    b = _memory(server, "beta")
    server.memory_link(a, b, "contradicts", role="out-older")
    server.memory_link(b, a, "contradicts", role="in-newer")

    [row] = server.memory_contradictions()

    assert row["edge"]["role"] == "in-newer"
    assert (row["a"]["slug"], row["b"]["slug"]) == (b, a)


@requires_omnigraph
def test_newest_link_first(server):
    a = _memory(server, "alpha")
    b = _memory(server, "beta")
    c = _memory(server, "gamma")
    server.memory_link(a, b, "contradicts", role="first")
    server.memory_link(a, c, "contradicts", role="second")

    roles = [row["edge"]["role"] for row in server.memory_contradictions()]

    assert roles == ["second", "first"]


@requires_omnigraph
def test_repo_scoping_keeps_a_pair_that_touches_the_repo(server):
    """A contradiction between two repos concerns both, so it shows in each."""
    here = _memory(server, "here")
    there = _memory(server, "there", repo=ELSEWHERE)
    there_too = _memory(server, "there too", repo=ELSEWHERE)
    server.memory_link(here, there, "contradicts")
    server.memory_link(there, there_too, "contradicts")

    assert _pairs(server.memory_contradictions(repo=HERE)) == [{here, there}]
    assert sorted(_pairs(server.memory_contradictions(repo=ELSEWHERE)), key=sorted) == (
        sorted([{here, there}, {there, there_too}], key=sorted)
    )
    assert len(server.memory_contradictions(repo="")) == 2


@requires_omnigraph
def test_repo_defaults_to_the_detected_repo(server):
    here = _memory(server, "here")
    there = _memory(server, "there", repo=ELSEWHERE)
    there_too = _memory(server, "there too", repo=ELSEWHERE)
    server.memory_link(here, there, "contradicts")
    server.memory_link(there, there_too, "contradicts")

    # conftest sets WITAN_REPO to HERE.
    assert _pairs(server.memory_contradictions()) == [{here, there}]


@requires_omnigraph
def test_no_repo_detected_keeps_only_pairs_touching_an_unscoped_memory(
    server, monkeypatch
):
    """Nothing detected is not the same as all repos: a caller with no repo
    context sees only what nobody scoped, as with `memory_list`."""
    unscoped = _memory(server, "unscoped", repo="")
    here = _memory(server, "here")
    there = _memory(server, "there", repo=ELSEWHERE)
    server.memory_link(unscoped, here, "contradicts")
    server.memory_link(here, there, "contradicts")

    # An empty WITAN_REPO disables detection entirely.
    monkeypatch.setenv("WITAN_REPO", "")

    assert _pairs(server.memory_contradictions()) == [{unscoped, here}]


@requires_omnigraph
def test_superseding_either_side_resolves_the_pair(server):
    a = _memory(server, "alpha")
    b = _memory(server, "beta")
    c = _memory(server, "gamma")
    d = _memory(server, "delta")
    server.memory_link(a, b, "contradicts")
    server.memory_link(c, d, "contradicts")
    replacement = _memory(server, "alpha, corrected")
    server.memory_link(replacement, a, "supersedes")

    assert _pairs(server.memory_contradictions(repo="")) == [{c, d}]
    assert {c, d} in _pairs(
        server.memory_contradictions(repo="", include_superseded=True)
    )
    assert {a, b} in _pairs(
        server.memory_contradictions(repo="", include_superseded=True)
    )


@requires_omnigraph
def test_agrees_with_recall_on_a_superseded_pair(server):
    a = _memory(server, "shared words alpha")
    b = _memory(server, "shared words beta")
    server.memory_link(a, b, "contradicts")
    server.memory_link(_memory(server, "shared words gamma"), a, "supersedes")

    assert server.recall(query="shared words", repo="")["contradictions"] == []
    assert server.memory_contradictions(repo="") == []
