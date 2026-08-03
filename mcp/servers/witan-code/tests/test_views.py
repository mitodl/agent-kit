"""Branch-view names: one writer per view, readable by everyone.

The bug this scheme replaces: `branch_store_name(current_branch())` derives a
view name from the git branch alone, so on a shared graph two checkouts of
`feature-x` write the SAME view and overwrite each other with different
working-tree states.
"""

import pytest

from witan_code import views


# ── Composition ──────────────────────────────────────────────────────────────


def test_two_writers_on_one_git_branch_get_different_views():
    """The whole point. Same branch, same repo, different writer, no collision."""
    assert views.repo_view("feature-x", actor="act-alice") != views.repo_view(
        "feature-x", actor="act-bob"
    )


def test_a_local_view_keeps_the_name_it_already_had():
    """No actor, no prefix: existing local stores need no migration and
    `branches --prune` keeps comparing like with like."""
    assert views.repo_view("feature-x", actor=None) == "feature-x"


def test_the_actor_comes_first_so_ownership_is_a_prefix():
    """Both stores put the owner first, which is what lets one write guard and
    one reaper cover both. Not a Cedar rule: omnigraph 0.8.1 has no branch-name
    predicate to hang `startsWith(branch, actor + "/")` on, so the prefix is
    enforced client-side only — see witan/docs/adr/0006."""
    assert views.repo_view("feature-x", actor="act-alice").startswith("act-alice/")
    assert views.bridge_view(
        "feature-x", "https://github.com/test/a", actor="act-alice"
    ).startswith("act-alice/")


def test_the_bridge_keeps_its_repo_qualifier():
    """One bridge graph carries every repo's bindings, so `feature-x` in two
    repos must stay apart there even for a single writer."""
    a = views.bridge_view("feature-x", "https://github.com/test/a", actor="act-alice")
    b = views.bridge_view("feature-x", "https://github.com/test/b", actor="act-alice")
    assert a != b
    assert a == "act-alice/https_github.com_test_a/feature-x"


def test_a_local_bridge_view_is_repo_qualified_but_unowned():
    assert (
        views.bridge_view("feature-x", "https://github.com/test/a", actor=None)
        == "https_github.com_test_a/feature-x"
    )


# ── Ownership ────────────────────────────────────────────────────────────────


def test_owner_reads_both_schemes_without_being_told_which():
    """The write guard doesn't know which graph it is writing to."""
    assert views.owner("act-alice/feature-x") == "act-alice"
    assert views.owner("act-alice/https_github.com_test_a/feature-x") == "act-alice"


def test_an_unowned_view_has_no_owner():
    assert views.owner("feature-x") is None
    assert views.owner("https_github.com_test_a/feature-x") is None


def test_a_branch_named_like_an_actor_is_not_an_owner():
    """`git checkout -b act-foo` produces a one-component view. Reading it as
    an owner would make it look writable by an actor that does not exist."""
    assert views.owner("act-foo") is None
    assert views.parse_view("act-foo").branch == "act-foo"


# ── Decomposition ────────────────────────────────────────────────────────────


def test_a_view_name_round_trips_through_parse():
    for name in (
        "feature-x",
        "act-alice/feature-x",
        "_detached",
        "act-alice/_main",
    ):
        assert views.parse_view(name).name == name
    for name in (
        "https_github.com_test_a/feature-x",
        "act-alice/https_github.com_test_a/feature-x",
    ):
        assert views.parse_view(name, bridge=True).name == name


def test_parse_splits_a_bridge_view_into_owner_repo_and_branch():
    parsed = views.parse_view(
        "act-alice/https_github.com_test_a/feature-x", bridge=True
    )
    assert (parsed.actor, parsed.repo, parsed.branch) == (
        "act-alice",
        "https_github.com_test_a",
        "feature-x",
    )


def test_an_unrecognized_shape_stays_opaque_rather_than_raising():
    """`branch list` returns whatever is in the store, including names no
    version of witan-code wrote."""
    parsed = views.parse_view("a/b/c/d", bridge=True)
    assert parsed.branch == "a/b/c/d"
    assert parsed.actor is None


# ── Enumerating every writer's view of one branch ────────────────────────────


def test_views_for_branch_finds_every_writer_including_unowned():
    """The read payoff of keeping branch views on the shared graph: see what
    your teammates have in flight, not just your own view."""
    names = [
        "main",
        "act-alice/feature-x",
        "act-bob/feature-x",
        "feature-x",
        "act-alice/other",
    ]

    found = views.views_for_branch(names, "feature-x")

    assert [v.name for v in found] == [
        "act-alice/feature-x",
        "act-bob/feature-x",
        "feature-x",  # un-owned sorts LAST — see the test below
    ]


def test_an_unowned_view_is_the_last_fallback_not_the_first():
    """Callers take `candidates[0]` as a read fallback. On a shared graph an
    un-owned view is one left behind from before namespacing — the
    collision-prone name this scheme replaced — so a read must prefer any
    single-writer view over it, not the other way round."""
    names = ["feature-x", "act-bob/feature-x"]

    assert views.views_for_branch(names, "feature-x")[0].name == "act-bob/feature-x"

    bridge_names = [
        "https_github.com_test_a/feature-x",
        "act-bob/https_github.com_test_a/feature-x",
    ]
    found = views.views_for_branch(bridge_names, "feature-x", bridge=True)
    assert found[0].name == "act-bob/https_github.com_test_a/feature-x"


def test_views_for_branch_takes_the_already_mapped_component():
    """`branch_store_name` is NOT idempotent — `_detached` sanitizes to
    `detached` — so mapping happens once, at the edge that sees a raw name."""
    assert [v.name for v in views.views_for_branch(["_detached"], "_detached")] == [
        "_detached"
    ]


def test_views_for_branch_can_scope_the_bridge_to_one_repo():
    names = [
        "act-alice/https_github.com_test_a/feature-x",
        "act-alice/https_github.com_test_b/feature-x",
    ]

    found = views.views_for_branch(
        names, "feature-x", bridge=True, repo="https://github.com/test/a"
    )

    assert [v.name for v in found] == ["act-alice/https_github.com_test_a/feature-x"]


def test_views_for_branch_is_empty_when_nobody_has_indexed_it():
    assert views.views_for_branch(["main", "act-alice/other"], "feature-x") == []


# ── The Layer-1 hop: CodeBranch's raw git branch reaches a view ──────────────


def test_a_raw_git_branch_maps_to_the_view_the_indexer_wrote():
    """witan's CodeBranch holds the RAW git branch (`feature/new-api`), never
    a storage name; a consumer sanitizes at the edge to reach the view. That
    only works if the two ends agree on the mapping — this pins that they do.
    """
    from witan_code import repo as repo_module

    component = repo_module.branch_store_name("feature/new-api")
    assert views.repo_view(component, actor="act-alice") == (
        "act-alice/feature_new-api"
    )
    assert (
        views.views_for_branch(["act-alice/feature_new-api"], component)[0].name
        == "act-alice/feature_new-api"
    )


def test_a_branch_named_main_still_avoids_the_stores_default_view():
    """`_main` (a non-default git branch literally named `main`) survives
    being wrapped in an owner."""
    from witan_code import repo as repo_module

    component = repo_module.branch_store_name("main")
    assert component == "_main"
    assert views.repo_view(component, actor="act-alice") == "act-alice/_main"


@pytest.mark.parametrize("actor", ["act-alice", None])
def test_the_separator_never_appears_inside_a_component(actor):
    """Components are sanitizer-guaranteed separator-free, which is what makes
    a name splittable back into its parts."""
    name = views.bridge_view(
        "feature_new-api", "https://github.com/test/a", actor=actor
    )
    expected = 3 if actor else 2
    assert len(name.split(views.SEPARATOR)) == expected
