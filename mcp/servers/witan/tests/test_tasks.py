"""End-to-end tests for the dependency-aware task tracker."""

import pytest

from .conftest import requires_omnigraph


@requires_omnigraph
def test_create_defaults(server):
    res = server.task_create(title="do a thing", description="x")
    assert res["slug"].startswith("tk-")
    assert res["status"] == "open"
    assert res["similar"] == []
    node = server.task_get(res["slug"])
    assert node["type"] == "task"
    assert node["priority"] == "p2"


# ── Task search (tk-phase-0-bm25-task-search-project-search) ────────────────


@requires_omnigraph
def test_task_search_bm25_ranked(server):
    server.task_create(title="uv usage", description="always use uv for python venvs")
    server.task_create(title="no raw sql", description="avoid raw sql in django views")

    hits = server.task_search("uv virtual environments")
    assert hits and hits[0]["title"] == "uv usage"


@requires_omnigraph
def test_task_search_finds_title_only_terms(server):
    t = server.task_create(
        title="zebrafish quokka narwhal",
        description="totally unrelated prose about compaction and fragments",
    )

    hits = server.task_search("zebrafish quokka narwhal")
    assert [h["slug"] for h in hits] == [t["slug"]]


@requires_omnigraph
def test_task_search_dedups_both_field_matches(server):
    t = server.task_create(
        title="quokka narwhal", description="more about the quokka narwhal"
    )

    hits = server.task_search("quokka narwhal")
    assert [h["slug"] for h in hits].count(t["slug"]) == 1


@requires_omnigraph
def test_task_search_includes_closed_tasks_by_default(server):
    """Unlike the project search default, a closed task is still a useful
    dedup signal (the work may already be done) — no status filter by default."""
    t = server.task_create(title="quokka narwhal fix", description="quokka narwhal")
    server.task_close(t["slug"], resolution="done")

    hits = server.task_search("quokka narwhal")
    assert t["slug"] in [h["slug"] for h in hits]

    open_only = server.task_search("quokka narwhal", status="open")
    assert t["slug"] not in [h["slug"] for h in open_only]


@requires_omnigraph
def test_task_search_scopes_by_repo(server):
    server.task_create(
        title="quokka narwhal in repo a",
        description="quokka narwhal",
        repo="https://github.com/test/a",
    )
    other = server.task_create(
        title="quokka narwhal in repo b",
        description="quokka narwhal",
        repo="https://github.com/test/b",
    )

    hits = server.task_search("quokka narwhal", repo="https://github.com/test/b")
    assert [h["slug"] for h in hits] == [other["slug"]]


@requires_omnigraph
def test_task_search_includes_unscoped_tasks_alongside_repo_scope(server):
    """A repo-scoped search must not drop unscoped (repo=None) matches — same
    convention task_list already follows (server.py:5518)."""
    scoped = server.task_create(
        title="quokka narwhal in repo a",
        description="quokka narwhal",
        repo="https://github.com/test/a",
    )
    unscoped = server.task_create(
        title="quokka narwhal unscoped", description="x", repo=""
    )

    hits = {
        h["slug"]
        for h in server.task_search("quokka narwhal", repo="https://github.com/test/a")
    }
    assert scoped["slug"] in hits
    assert unscoped["slug"] in hits


@requires_omnigraph
def test_task_create_returns_similar_tasks(server):
    existing = server.task_create(
        title="fix the flaky retry test", description="the retry test flakes on CI"
    )

    created = server.task_create(
        title="fix the flaky retry test again",
        description="the retry test still flakes on CI",
    )
    assert existing["slug"] in [s["slug"] for s in created["similar"]]
    assert len(created["similar"]) <= 3


@requires_omnigraph
def test_blocking_and_ready(server):
    proj = server.workflow_project_create(title="P", description="d", phase="spec")
    a = server.task_create(
        title="blocker A", description="first", priority="p0", project_slug=proj["slug"]
    )
    b = server.task_create(
        title="dependent B",
        description="needs A",
        priority="p1",
        project_slug=proj["slug"],
        blocked_by=[a["slug"]],
        external_uri="https://github.com/test/repo/issues/1",
    )
    assert a["status"] == "open"
    assert b["status"] == "blocked"

    ready = {t["slug"] for t in server.task_ready(project_slug=proj["slug"])}
    assert a["slug"] in ready
    assert b["slug"] not in ready

    # closing the blocker auto-unblocks B and makes it ready
    closed = server.task_close(a["slug"], resolution="done")
    assert closed["status"] == "closed"
    assert closed["closed_at"]
    assert server.task_get(b["slug"])["status"] == "open"

    ready2 = {t["slug"] for t in server.task_ready(project_slug=proj["slug"])}
    assert b["slug"] in ready2

    # external_uri persisted
    assert (
        server.task_get(b["slug"])["external_uri"]
        == "https://github.com/test/repo/issues/1"
    )


@requires_omnigraph
def test_ready_priority_order(server):
    proj = server.workflow_project_create(title="P", description="d")
    server.task_create(
        title="low", description="x", priority="p3", project_slug=proj["slug"]
    )
    server.task_create(
        title="high", description="x", priority="p0", project_slug=proj["slug"]
    )
    ready = server.task_ready(project_slug=proj["slug"])
    assert [t["priority"] for t in ready] == sorted(t["priority"] for t in ready)
    assert ready[0]["priority"] == "p0"


@requires_omnigraph
def test_hierarchy_epic_children(server):
    proj = server.workflow_project_create(title="P", description="d")
    epic = server.task_create(
        title="big epic", description="parent", type="epic", project_slug=proj["slug"]
    )
    child = server.task_create(
        title="sub issue",
        description="child",
        parent=epic["slug"],
        project_slug=proj["slug"],
    )
    assert server.task_get(epic["slug"])["type"] == "epic"
    assert server.task_get(child["slug"])["parent_slug"] == epic["slug"]
    kids = {t["slug"] for t in server.task_list(parent=epic["slug"])}
    assert child["slug"] in kids


@requires_omnigraph
def test_link_blocks_after_the_fact(server):
    a = server.task_create(title="A", description="x")
    b = server.task_create(title="B", description="y")
    server.task_link(a["slug"], b["slug"], kind="blocks")
    nb = server.task_get(b["slug"])
    assert a["slug"] in (nb["blocked_by"] or [])
    assert nb["status"] == "blocked"


@requires_omnigraph
def test_update_claim_and_list_status(server):
    t = server.task_create(title="claimable", description="x")
    server.task_update(t["slug"], status="in_progress", assignee="alice")
    node = server.task_get(t["slug"])
    assert node["status"] == "in_progress"
    assert node["assignee"] == "alice"
    in_progress = {x["slug"] for x in server.task_list(status="in_progress")}
    assert t["slug"] in in_progress


@requires_omnigraph
def test_create_with_already_closed_blocker_is_open(server):
    # If every blocker is already closed, a new task is ready now, not blocked.
    a = server.task_create(title="done blocker", description="x")
    server.task_close(a["slug"])
    b = server.task_create(
        title="depends on done", description="x", blocked_by=[a["slug"]]
    )
    assert b["status"] == "open"
    assert b["slug"] in {t["slug"] for t in server.task_ready()}


@requires_omnigraph
def test_update_to_closed_unblocks_dependents(server):
    a = server.task_create(title="blocker", description="x")
    b = server.task_create(title="dependent", description="x", blocked_by=[a["slug"]])
    assert b["status"] == "blocked"
    # Closing via task_update (not task_close) must still auto-unblock dependents.
    server.task_update(a["slug"], status="closed")
    assert server.task_get(b["slug"])["status"] == "open"
    assert b["slug"] in {t["slug"] for t in server.task_ready()}


# ── Unlinking (task_unlink) ────────────────────────────────────────


@requires_omnigraph
def test_unlink_blocks_clears_field_and_reopens(server):
    a = server.task_create(title="blocker", description="x")
    b = server.task_create(title="blocked", description="y")
    server.task_link(a["slug"], b["slug"], kind="blocks")
    assert server.task_get(b["slug"])["status"] == "blocked"

    res = server.task_unlink(a["slug"], b["slug"], kind="blocks")
    assert res["removed"] is True
    nb = server.task_get(b["slug"])
    assert not (nb["blocked_by"] or [])
    # Nothing open is holding it any more, so it is ready work again.
    assert nb["status"] == "open"
    assert b["slug"] in {t["slug"] for t in server.task_ready()}


@requires_omnigraph
def test_unlink_one_of_several_blockers_keeps_the_rest(server):
    """Several blockers on the target, one removed.

    `from` has a single outbound edge here, so this takes the delete-by-`from`
    path with nothing to re-insert. `test_unlink_reinserts_survivors` is the
    one that exercises re-insertion.
    """
    a = server.task_create(title="blocker A", description="x")
    b = server.task_create(title="blocker B", description="x")
    c = server.task_create(title="blocker C", description="x")
    t = server.task_create(title="target", description="y")
    for blocker in (a, b, c):
        server.task_link(blocker["slug"], t["slug"], kind="blocks")

    server.task_unlink(b["slug"], t["slug"], kind="blocks")

    node = server.task_get(t["slug"])
    assert set(node["blocked_by"] or []) == {a["slug"], c["slug"]}
    # Still genuinely blocked — two open blockers remain.
    assert node["status"] == "blocked"
    assert t["slug"] not in {x["slug"] for x in server.task_ready()}


@requires_omnigraph
def test_unlink_leaves_other_tasks_edges_alone(server):
    """Deleting by the shared endpoint must not disturb an unrelated task that
    happens to share a blocker."""
    blocker = server.task_create(title="shared blocker", description="x")
    keep = server.task_create(title="keeps its blocker", description="y")
    drop = server.task_create(title="loses its blocker", description="z")
    server.task_link(blocker["slug"], keep["slug"], kind="blocks")
    server.task_link(blocker["slug"], drop["slug"], kind="blocks")

    server.task_unlink(blocker["slug"], drop["slug"], kind="blocks")

    assert blocker["slug"] in (server.task_get(keep["slug"])["blocked_by"] or [])
    assert not (server.task_get(drop["slug"])["blocked_by"] or [])


@requires_omnigraph
def test_unlink_reinserts_survivors(server):
    """The re-insert branch, which only runs when BOTH endpoints are crowded.

    A single-predicate delete takes out every edge on the chosen endpoint, so
    when that endpoint has more than the target edge the rest must be put back.
    Shape: `b` blocks two tasks, and the target is blocked by three — so the
    smaller side (`b`'s two outbound edges) is deleted and one is restored.
    The other tests all happen to have a side of size one and never reach here.
    """
    a = server.task_create(title="blocker A", description="x")
    b = server.task_create(title="blocker B", description="x")
    c = server.task_create(title="blocker C", description="x")
    target = server.task_create(title="target", description="y")
    other = server.task_create(title="also blocked by B", description="y")

    for blocker in (a, b, c):
        server.task_link(blocker["slug"], target["slug"], kind="blocks")
    server.task_link(b["slug"], other["slug"], kind="blocks")

    server.task_unlink(b["slug"], target["slug"], kind="blocks")

    # The intended edge is gone.
    assert set(server.task_get(target["slug"])["blocked_by"] or []) == {
        a["slug"],
        c["slug"],
    }
    # And the sibling edge deleted as collateral came back.
    assert b["slug"] in (server.task_get(other["slug"])["blocked_by"] or [])
    assert server.task_get(other["slug"])["status"] == "blocked"
    # Re-running finds nothing, proving the restore did not duplicate it.
    assert (
        server.task_unlink(b["slug"], target["slug"], kind="blocks")["removed"] is False
    )
    assert (
        server.task_unlink(b["slug"], other["slug"], kind="blocks")["removed"] is True
    )


@requires_omnigraph
def test_unlink_is_idempotent_and_reports_absence(server):
    a = server.task_create(title="A", description="x")
    b = server.task_create(title="B", description="y")
    # Never linked at all.
    assert server.task_unlink(a["slug"], b["slug"], kind="blocks")["removed"] is False

    server.task_link(a["slug"], b["slug"], kind="blocks")
    assert server.task_unlink(a["slug"], b["slug"], kind="blocks")["removed"] is True
    # Second removal is a no-op, not an error.
    assert server.task_unlink(a["slug"], b["slug"], kind="blocks")["removed"] is False


@requires_omnigraph
def test_unlink_wrong_direction_leaves_the_correct_edge(server):
    """The case this was built for: a link recorded backwards, removed without
    disturbing the correct one pointing the other way."""
    a = server.task_create(title="real blocker", description="x")
    b = server.task_create(title="blocked", description="y")
    server.task_link(a["slug"], b["slug"], kind="blocks")  # correct
    server.task_link(b["slug"], a["slug"], kind="blocks")  # backwards

    server.task_unlink(b["slug"], a["slug"], kind="blocks")

    assert not (server.task_get(a["slug"])["blocked_by"] or [])
    assert a["slug"] in (server.task_get(b["slug"])["blocked_by"] or [])


@requires_omnigraph
def test_unlink_parent_clears_parent_slug(server):
    epic = server.task_create(title="epic", description="x", type="epic")
    child = server.task_create(title="child", description="y", parent=epic["slug"])
    assert server.task_get(child["slug"])["parent_slug"] == epic["slug"]

    server.task_unlink(epic["slug"], child["slug"], kind="parent")

    assert server.task_get(child["slug"])["parent_slug"] is None
    assert child["slug"] not in {
        t["slug"] for t in server.task_list(parent=epic["slug"])
    }


@requires_omnigraph
def test_unlink_addresses_removes_the_memory_link(server):
    t = server.task_create(title="fixes it", description="x")
    m = server.memory_store(kind="lesson", title="a lesson", content="c")
    server.task_link(t["slug"], m["slug"], kind="addresses")

    def seeded_by_task() -> set[str]:
        # `recall(task=…)` seeds from the memories a task Addresses, so it is
        # the observable surface for this edge.
        return {x["slug"] for x in server.recall(task=t["slug"])["memories"]}

    assert m["slug"] in seeded_by_task()

    res = server.task_unlink(t["slug"], m["slug"], kind="addresses")
    assert res["removed"] is True
    assert m["slug"] not in seeded_by_task()


@requires_omnigraph
def test_project_unblock_deletes_the_edge_not_just_the_field(server):
    a = server.workflow_project_create(title="PA", description="d")
    b = server.workflow_project_create(title="PB", description="d")
    server.workflow_project_block(slug=a["slug"], blocks_slug=b["slug"])
    assert a["slug"] in (server.workflow_project_get(b["slug"])["blocked_by"] or [])

    server.workflow_project_unblock(slug=a["slug"], blocks_slug=b["slug"])

    assert not (server.workflow_project_get(b["slug"])["blocked_by"] or [])
    # The edge is gone too, so re-blocking then unblocking stays consistent
    # rather than accumulating duplicates.
    server.workflow_project_block(slug=a["slug"], blocks_slug=b["slug"])
    server.workflow_project_unblock(slug=a["slug"], blocks_slug=b["slug"])
    assert not (server.workflow_project_get(b["slug"])["blocked_by"] or [])


@requires_omnigraph
def test_link_closed_blocker_does_not_block(server):
    a = server.task_create(title="closed", description="x")
    server.task_close(a["slug"])
    b = server.task_create(title="open task", description="x")
    server.task_link(a["slug"], b["slug"], kind="blocks")
    # Linking an already-closed blocker must not flip the task to blocked.
    assert server.task_get(b["slug"])["status"] == "open"


# ── Advisory claims (Option A) ─────────────────────────────────────


@requires_omnigraph
def test_claim_removes_from_ready_and_refuses_double(server):
    t = server.task_create(title="claimable", description="x")
    assert t["slug"] in {r["slug"] for r in server.task_ready()}

    c = server.task_claim(t["slug"], assignee="agentA")
    assert c["claimed"] is True
    assert server.task_get(t["slug"])["status"] == "in_progress"
    # a claimed task drops out of ready work
    assert t["slug"] not in {r["slug"] for r in server.task_ready()}

    # a different agent can't take a live claim
    c2 = server.task_claim(t["slug"], assignee="agentB")
    assert c2["claimed"] is False
    assert c2["held_by"] == "agentA"

    # the holder can renew its own claim (idempotent)
    assert server.task_claim(t["slug"], assignee="agentA")["claimed"] is True

    # force steals it
    c3 = server.task_claim(t["slug"], assignee="agentB", force=True)
    assert c3["claimed"] is True and c3["stole"] is True


@requires_omnigraph
def test_release_returns_task_to_ready(server):
    t = server.task_create(title="rel", description="x")
    server.task_claim(t["slug"], assignee="agentA")
    rel = server.task_release(t["slug"], assignee="agentA")
    assert rel["released"] is True

    node = server.task_get(t["slug"])
    assert node["status"] == "open"
    assert node["assignee"] is None
    assert node["claimed_at"] is None
    assert t["slug"] in {r["slug"] for r in server.task_ready()}


@requires_omnigraph
def test_release_refuses_other_holder(server):
    t = server.task_create(title="rel2", description="x")
    server.task_claim(t["slug"], assignee="agentA")
    rel = server.task_release(t["slug"], assignee="agentB")
    assert rel["released"] is False
    assert rel["held_by"] == "agentA"


@requires_omnigraph
def test_claim_refuses_blocked(server):
    a = server.task_create(title="A", description="x")
    b = server.task_create(title="B", description="x", blocked_by=[a["slug"]])
    assert server.task_claim(b["slug"])["reason"] == "blocked"


@requires_omnigraph
def test_claim_refuses_closed(server):
    t = server.task_create(title="C", description="x")
    server.task_close(t["slug"])
    assert server.task_claim(t["slug"])["reason"] == "closed"


@requires_omnigraph
def test_expired_lease_is_reclaimable(server, monkeypatch):
    from witan import readiness

    # Make any claim's lease count as elapsed immediately. The lease constant is
    # shared by task_ready and the context hook, so it lives in `readiness`.
    monkeypatch.setattr(readiness, "CLAIM_LEASE_SECONDS", -1)

    t = server.task_create(title="leasey", description="x")
    server.task_claim(t["slug"], assignee="agentA")

    # an abandoned (lease-expired) in_progress task resurfaces as ready …
    assert t["slug"] in {r["slug"] for r in server.task_ready()}
    # … and another agent can reclaim it without force. This is recovery of an
    # abandoned task, not stealing a live claim, so `stole` stays False.
    c = server.task_claim(t["slug"], assignee="agentB")
    assert c["claimed"] is True
    assert c["stole"] is False
    assert server.task_get(t["slug"])["assignee"] == "agentB"


@requires_omnigraph
def test_task_update_to_in_progress_stamps_claimed_at(server):
    # Regression: task_update(status="in_progress") — the /witan-task skill's,
    # the CLI's, and every "mark this started" path's route — used to leave
    # claimed_at null forever, which made the task read as instantly and
    # permanently free (see test_readiness.py for the unit-level case).
    t = server.task_create(title="marked started", description="x")
    server.task_update(t["slug"], status="in_progress")
    node = server.task_get(t["slug"])
    assert node["status"] == "in_progress"
    assert node["claimed_at"] is not None


@requires_omnigraph
def test_task_update_to_in_progress_defaults_a_missing_assignee(server):
    # tk-task-update-can-still-manufacture-an-unnameable--9ba86a: this used to
    # stamp claimed_at without ever setting assignee, reaching exactly the
    # state task_claim correctly refuses and correctly cannot name a holder
    # for — a live lease with nobody on record.
    from witan import server as srv

    t = server.task_create(title="marked started, no assignee given", description="x")
    server.task_update(t["slug"], status="in_progress")
    node = server.task_get(t["slug"])
    assert node["assignee"] == srv._current_author()


@requires_omnigraph
def test_task_update_to_in_progress_does_not_clobber_an_existing_assignee(server):
    # Only fills a GAP. A colleague marking someone else's task in_progress to
    # log status must not silently reassign it to themselves.
    t = server.task_create(title="already held", description="x")
    server.task_claim(t["slug"], assignee="original-holder")

    server.task_update(t["slug"], status="in_progress")

    assert server.task_get(t["slug"])["assignee"] == "original-holder"


@requires_omnigraph
def test_task_update_to_in_progress_respects_an_explicit_assignee(server):
    t = server.task_create(title="explicit assignee", description="x")
    server.task_update(t["slug"], status="in_progress", assignee="explicit-holder")
    assert server.task_get(t["slug"])["assignee"] == "explicit-holder"


@requires_omnigraph
def test_task_update_to_in_progress_treats_a_blank_assignee_as_missing(server):
    # The gap the first fix left, caught by Copilot as a suppressed review
    # comment on #283 — which files no thread, so it did not show up in the
    # unresolved count and was nearly missed.
    #
    # `_claim_holder` reads a blank assignee as missing (`if assignee:`) while
    # the write path tested `is not None`, so an explicit "" was written
    # straight through AND skipped the default: claimed_at stamped, no
    # nameable holder. Exactly the state this task exists to make
    # unrepresentable, reachable through the parameter meant to prevent it.
    from witan import server as srv

    t = server.task_create(title="blank assignee, marked started", description="x")
    server.task_update(t["slug"], status="in_progress", assignee="")
    node = server.task_get(t["slug"])
    assert node["assignee"] == srv._current_author()
    assert node["claimed_at"]


@requires_omnigraph
def test_task_update_to_in_progress_treats_a_whitespace_assignee_as_missing(server):
    # Whitespace names nobody either, and a holder is both shown to humans and
    # matched by _holder_matches, so " " must not survive as an identity.
    from witan import server as srv

    t = server.task_create(title="whitespace assignee", description="x")
    server.task_update(t["slug"], status="in_progress", assignee="   ")
    assert server.task_get(t["slug"])["assignee"] == srv._current_author()


@requires_omnigraph
def test_task_update_blank_assignee_does_not_clear_an_existing_holder(server):
    # Blank means "not provided", not "unassign". A task that already has a
    # holder keeps it — the same gap-filling-only rule the non-blank path
    # follows, and the reason this normalises to None rather than erroring.
    t = server.task_create(title="held, blank passed", description="x")
    server.task_claim(t["slug"], assignee="original-holder")

    server.task_update(t["slug"], status="in_progress", assignee="")

    assert server.task_get(t["slug"])["assignee"] == "original-holder"


@requires_omnigraph
@requires_omnigraph
def test_task_claim_reports_qualified_when_session_id_given(server, monkeypatch):
    """A claim that names a session says so, so callers can check."""
    from witan import server as srv

    monkeypatch.setattr(srv, "_is_local_stdio", lambda: False)
    t = server.task_create(title="qualified claim", description="x")
    result = server.task_claim(t["slug"], session_id="sess-abcdefgh")

    assert result["claimed"] is True
    assert result["qualified"] is True
    assert "warning" not in result


@requires_omnigraph
def test_task_claim_deployed_without_session_id_warns(server, monkeypatch):
    """The uncovered path: an agent calling a deployed witan directly.

    It cannot be refused — the server has no way to supply the id — so the
    contract is that it is visible rather than silent.
    """
    from witan import server as srv

    monkeypatch.setattr(srv, "_is_local_stdio", lambda: False)
    t = server.task_create(title="unqualified claim", description="x")
    result = server.task_claim(t["slug"])

    assert result["claimed"] is True
    assert result["qualified"] is False
    assert "session_id" in result["warning"]


@requires_omnigraph
def test_task_claim_explicit_assignee_is_not_warned_about(server, monkeypatch):
    """An explicit assignee is a deliberate choice (a worker name, a CI job).

    Warning about it would fire on every such caller forever, and there is
    nothing for them to fix.
    """
    from witan import server as srv

    monkeypatch.setattr(srv, "_is_local_stdio", lambda: False)
    t = server.task_create(title="worker claim", description="x")
    result = server.task_claim(t["slug"], assignee="ci-runner-7")

    assert result["claimed"] is True
    assert result["qualified"] is False
    assert "warning" not in result


@requires_omnigraph
def test_task_claim_deployed_with_empty_assignee_warns(server, monkeypatch):
    # `_claim_holder` reads `assignee=""` as missing (`if assignee:`), so the
    # warning predicate must use the same test — checking `assignee is None`
    # let an empty string through as if it were a deliberate explicit
    # assignee, silently skipping the warning for the exact unsafe case
    # (defaulted, unqualified holder) it exists to flag.
    from witan import server as srv

    monkeypatch.setattr(srv, "_is_local_stdio", lambda: False)
    t = server.task_create(title="empty-assignee claim", description="x")
    result = server.task_claim(t["slug"], assignee="")

    assert result["claimed"] is True
    assert result["qualified"] is False
    assert "session_id" in result["warning"]


@requires_omnigraph
def test_task_claim_session_id_outside_charset_still_qualifies(server, monkeypatch):
    # `_SESSION_SUFFIX_RE` only recognizes `[0-9A-Za-z_-]`. A session_id with
    # any other character (e.g. a `.` in a dotted run id) must still qualify
    # the holder — disallowed characters are stripped, not passed through
    # verbatim to silently produce an unrecognizable suffix.
    from witan import server as srv

    monkeypatch.setattr(srv, "_is_local_stdio", lambda: False)
    t = server.task_create(title="dotted session id", description="x")
    result = server.task_claim(t["slug"], session_id="run.1234")

    assert result["claimed"] is True
    assert result["qualified"] is True
    assert "warning" not in result


def test_task_update_defaulted_assignee_is_qualified_by_session_id(server):
    # Same qualification task_claim applies — see
    # test_caller_supplied_session_id_beats_the_server_environment — so a
    # deployed caller marking its own work in_progress via task_update,
    # without a session in the server's own environment to fall back to,
    # still gets a holder two of its own parallel sessions cannot collide on.
    from witan import server as srv

    t = server.task_create(title="deployed caller", description="x")
    server.task_update(
        t["slug"],
        status="in_progress",
        session_id="cccccccc-9999-0000-1111-222222222222",
    )
    node = server.task_get(t["slug"])
    assert node["assignee"] == f"{srv._current_author()}#cccccccc"


@requires_omnigraph
def test_update_task_default_if_missing_does_not_override_a_real_assignee(server):
    """`_update_task`'s `default_if_missing` checks the row's CURRENT value —
    read by this same call, not decided earlier by the caller (see its
    docstring: a caller that precomputed the value off its own separate read
    could hand back a stale default that clobbers a real assignment made in
    between). A task already claimed for real must keep that assignee even
    when a `default_if_missing["assignee"]` factory is supplied.
    """
    from witan import server as srv

    t = server.task_create(title="already claimed", description="x")
    server.task_claim(t["slug"], assignee="real-claimant")

    srv._update_task(
        t["slug"],
        {"status": "in_progress"},
        default_if_missing={"assignee": lambda: "stale-default"},
    )

    assert server.task_get(t["slug"])["assignee"] == "real-claimant"


@requires_omnigraph
def test_update_task_default_if_missing_fills_a_genuine_gap(server):
    from witan import server as srv

    t = server.task_create(title="never claimed", description="x")

    srv._update_task(
        t["slug"],
        {"status": "in_progress"},
        default_if_missing={"assignee": lambda: "filled-in"},
    )

    assert server.task_get(t["slug"])["assignee"] == "filled-in"


@requires_omnigraph
def test_update_task_default_if_missing_never_overrides_an_explicit_change(server):
    from witan import server as srv

    t = server.task_create(title="explicit wins", description="x")

    srv._update_task(
        t["slug"],
        {"status": "in_progress", "assignee": "explicit"},
        default_if_missing={"assignee": lambda: "should-not-be-used"},
    )

    assert server.task_get(t["slug"])["assignee"] == "explicit"


@requires_omnigraph
def test_unleased_recent_in_progress_is_not_ready_or_claimable(server):
    """A task moved to in_progress with no assignee on record (e.g. a legacy row
    written before task_update stamped claimed_at) must still read as held while
    recently touched — not as free just because nobody can be named as holder."""
    from witan import server as srv

    t = server.task_create(title="ghost claim", description="x")
    srv._update_task(t["slug"], {"status": "in_progress", "claimed_at": None})
    node = server.task_get(t["slug"])
    assert node["assignee"] is None
    assert node["claimed_at"] is None

    assert t["slug"] not in {r["slug"] for r in server.task_ready()}

    c = server.task_claim(t["slug"], assignee="agentB")
    assert c["claimed"] is False
    # held_by must be a stable placeholder, not None — a null held_by is
    # ambiguous to API consumers (not held vs. held by an unknown holder).
    assert c["held_by"] == srv._UNKNOWN_HOLDER


@requires_omnigraph
def test_parallel_sessions_of_one_person_do_not_share_a_claim(server, monkeypatch):
    """The silent double-claim: two agent sessions run by the same human used to
    both be told ``claimed: True`` for one task.

    ``assignee`` defaulted to the bare identity, so ``current_holder != holder``
    was False for the second session, the contention branch never ran, and the
    write went through as a *renewal* of the first session's lease. Neither side
    saw a signal. The holder is now qualified with $CLAUDE_SESSION_ID, so the
    second session hits the ordinary held-by-someone-else path.
    """
    from witan import server as srv

    t = server.task_create(title="two sessions", description="x")

    monkeypatch.setenv("CLAUDE_SESSION_ID", "aaaaaaaa-1111-2222-3333-444444444444")
    first = server.task_claim(t["slug"])
    assert first["claimed"] is True
    assert first["assignee"] == f"{srv._current_author()}#aaaaaaaa"

    monkeypatch.setenv("CLAUDE_SESSION_ID", "bbbbbbbb-5555-6666-7777-888888888888")
    second = server.task_claim(t["slug"])
    assert second["claimed"] is False
    assert second["reason"] == "held"
    assert second["held_by"] == first["assignee"]

    # …and the first session's claim is intact, not renewed under the second.
    assert server.task_get(t["slug"])["assignee"] == first["assignee"]

    # The same session re-claiming is still an idempotent renewal.
    monkeypatch.setenv("CLAUDE_SESSION_ID", "aaaaaaaa-1111-2222-3333-444444444444")
    assert server.task_claim(t["slug"])["claimed"] is True


@requires_omnigraph
def test_caller_supplied_session_id_beats_the_server_environment(server, monkeypatch):
    """The deployed case: the server's own environment has no session id.

    A pod's env is `WITAN_*`/`KUBERNETES_*` and nothing else — it is not a
    child of the agent, so it never sees $CLAUDE_SESSION_ID. Reading the
    variable server-side therefore qualified nothing for any remote caller, and
    two of one person's concurrent sessions collided exactly as before. The id
    has to arrive as an argument; the environment is only the local-stdio
    fallback.
    """
    from witan import server as srv

    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)  # a deployed pod
    t = server.task_create(title="deployed claim", description="x")

    first = server.task_claim(t["slug"], session_id="11111111-aaaa")
    assert first["claimed"] is True
    assert first["assignee"] == f"{srv._current_author()}#11111111"

    second = server.task_claim(t["slug"], session_id="22222222-bbbb")
    assert second["claimed"] is False
    assert second["held_by"] == first["assignee"]

    # An explicit assignee still outranks both sources.
    assert (
        server.task_claim(
            t["slug"], assignee="ci-worker", session_id="333", force=True
        )["assignee"]
        == "ci-worker"
    )


def test_holder_qualifier_survives_rich_rendering(monkeypatch):
    """The qualifier is printed straight into a rich console by the task CLI.

    The first cut used ``"<identity> [<session>]"``, and rich read ``[aaaaaaaa]``
    as a style tag and dropped it — so both sessions rendered as the bare
    identity again, exactly the indistinguishability the qualifier removes, at
    the one place a human actually reads it. No markup-significant characters.
    """
    import io

    from rich.console import Console

    from witan import server as srv

    monkeypatch.setenv("CLAUDE_SESSION_ID", "aaaaaaaa-1111-2222-3333-444444444444")
    holder = srv._claim_holder()
    assert "aaaaaaaa" in holder

    buf = io.StringIO()
    Console(file=buf, width=200, no_color=True).print(holder)
    assert "aaaaaaaa" in buf.getvalue()


@requires_omnigraph
def test_holder_without_session_id_is_the_bare_identity(server, monkeypatch):
    """No $CLAUDE_SESSION_ID means one session, so the holder stays exactly what
    older stores already hold — no qualifier, nothing to migrate."""
    from witan import server as srv

    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    t = server.task_create(title="no session", description="x")
    assert server.task_claim(t["slug"])["assignee"] == srv._current_author()


@requires_omnigraph
def test_assignee_filters_match_a_person_across_their_sessions(server, monkeypatch):
    """The filter's own precision decides its scope.

    An unqualified filter means the person and must span their sessions; a
    qualified one names a single session and must NOT widen back to the person.
    Two sessions each hold a task here — with only one, an over-wide qualified
    filter still returns the right row and the test passes for the wrong
    reason, which is exactly how the first version of this shipped.
    """
    from witan import server as srv

    me = srv._current_author()

    monkeypatch.setenv("CLAUDE_SESSION_ID", "cccccccc-9999-0000-1111-222222222222")
    mine_c = server.task_create(title="held by session c", description="x")
    claimed_c = server.task_claim(mine_c["slug"])

    monkeypatch.setenv("CLAUDE_SESSION_ID", "eeee0000-9999-0000-1111-222222222222")
    mine_e = server.task_create(title="held by session e", description="x")
    claimed_e = server.task_claim(mine_e["slug"])

    assert claimed_c["assignee"] != claimed_e["assignee"]

    # Unqualified: the person, across both sessions.
    by_person = {r["slug"] for r in server.task_list(assignee=me)}
    assert {mine_c["slug"], mine_e["slug"]} <= by_person

    # Qualified: that one session, and not the person's other one.
    by_session_c = {r["slug"] for r in server.task_list(assignee=claimed_c["assignee"])}
    assert mine_c["slug"] in by_session_c
    assert mine_e["slug"] not in by_session_c

    # Someone else's filter must not pick either up.
    others = {r["slug"] for r in server.task_list(assignee="agentZ")}
    assert not ({mine_c["slug"], mine_e["slug"]} & others)


@requires_omnigraph
def test_release_accepts_another_session_of_the_same_person(server, monkeypatch):
    """Releasing a claim your other session took is not a steal — same person,
    different session — so it must not require force."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "dddddddd-0000-0000-0000-000000000000")
    t = server.task_create(title="handover", description="x")
    server.task_claim(t["slug"])

    monkeypatch.setenv("CLAUDE_SESSION_ID", "eeeeeeee-0000-0000-0000-000000000000")
    assert server.task_release(t["slug"])["released"] is True
    assert server.task_get(t["slug"])["assignee"] is None

    # A different person still needs force, and is told so.
    server.task_claim(t["slug"], assignee="agentA")
    refused = server.task_release(t["slug"], assignee="agentB")
    assert refused["released"] is False
    assert "--force" in refused["remedy"]


@requires_omnigraph
def test_refused_claim_names_a_way_out(server):
    """A refusal that only names the holder is a dead end — especially when
    there is no holder to name. Every refusal carries the recovery command."""
    from witan import server as srv

    t = server.task_create(title="stuck", description="x")
    srv._update_task(t["slug"], {"status": "in_progress", "claimed_at": None})

    refused = server.task_claim(t["slug"], assignee="agentB")
    assert refused["claimed"] is False
    assert refused["held_by"] == srv._UNKNOWN_HOLDER
    remedy = refused["remedy"]
    assert f"witan task claim {t['slug']} --force" in remedy
    assert f"witan task release {t['slug']} --force" in remedy

    # And the way out actually works.
    assert (
        server.task_claim(t["slug"], assignee="agentB", force=True)["claimed"] is True
    )


@requires_omnigraph
def test_refused_claim_remedy_reports_when_the_lease_lapses(server):
    """The third way out is simply waiting, so the refusal says until when."""
    t = server.task_create(title="leased", description="x")
    server.task_claim(t["slug"], assignee="agentA")

    refused = server.task_claim(t["slug"], assignee="agentB")
    assert "held by agentA" in refused["remedy"]
    assert "lease lapses at" in refused["remedy"]


@requires_omnigraph
def test_unleased_stale_in_progress_is_still_reclaimable(server, monkeypatch):
    """The abandonment recovery path must not regress: an in_progress task with
    no claimed_at whose updated_at is older than the lease window is genuinely
    abandoned and must resurface as ready/claimable."""
    from witan import readiness
    from witan import server as srv

    monkeypatch.setattr(readiness, "CLAIM_LEASE_SECONDS", -1)

    t = server.task_create(title="abandoned ghost", description="x")
    srv._update_task(t["slug"], {"status": "in_progress", "claimed_at": None})

    assert t["slug"] in {r["slug"] for r in server.task_ready()}
    c = server.task_claim(t["slug"], assignee="agentB")
    assert c["claimed"] is True


# ── Best-effort CAS: conflict detection & post-write verification ───────


@requires_omnigraph
def test_claim_conflict_reports_lost_race_without_clobber(server, monkeypatch):
    """agentA reads the task as open, but a rival (agentB) commits its claim in
    the race window and agentA's write surfaces an OCC conflict. agentA must
    report ``lost_race`` and leave agentB's claim intact — not blindly re-apply."""
    from witan import graph as graph_mod
    from witan import server as srv

    t = server.task_create(title="contended", description="x")
    real_change = srv.client.change
    calls = {"n": 0}

    def flaky_change(*args, surface_conflict=False, **kwargs):
        if surface_conflict and calls["n"] == 0:
            calls["n"] += 1
            # rival wins the race: its claim lands first, then ours conflicts
            srv._update_task(
                t["slug"],
                {
                    "status": "in_progress",
                    "assignee": "agentB",
                    "claimed_at": srv.now_iso(),
                },
            )
            raise graph_mod.OmnigraphConflict("stale view")
        return real_change(*args, surface_conflict=surface_conflict, **kwargs)

    monkeypatch.setattr(srv.client, "change", flaky_change)

    res = server.task_claim(t["slug"], assignee="agentA")
    assert res["claimed"] is False
    assert res["reason"] == "lost_race"
    assert res["held_by"] == "agentB"
    # the losing claim did not overwrite the winner
    assert server.task_get(t["slug"])["assignee"] == "agentB"


@requires_omnigraph
def test_claim_post_write_verification_catches_last_writer(server, monkeypatch):
    """Our claim write succeeds, but a rival commits *after* it (last-write-wins
    with no store CAS). Post-write verification must detect that we no longer
    hold the task and report ``lost_race`` rather than a false success."""
    from witan import server as srv

    t = server.task_create(title="clobbered", description="x")
    real_change = srv.client.change
    calls = {"n": 0}

    def clobber_after(*args, surface_conflict=False, **kwargs):
        result = real_change(*args, surface_conflict=surface_conflict, **kwargs)
        if surface_conflict and calls["n"] == 0:
            calls["n"] += 1
            # rival's claim lands immediately after ours committed
            srv._update_task(
                t["slug"],
                {
                    "status": "in_progress",
                    "assignee": "agentB",
                    "claimed_at": srv.now_iso(),
                },
            )
        return result

    monkeypatch.setattr(srv.client, "change", clobber_after)

    res = server.task_claim(t["slug"], assignee="agentA")
    assert res["claimed"] is False
    assert res["reason"] == "lost_race"
    assert res["held_by"] == "agentB"


@requires_omnigraph
def test_claim_consecutive_conflicts_stay_surfaced_no_clobber(server, monkeypatch):
    """Two OCC conflicts in a row must both stay on the surfaced path — the
    retry after the first conflict must NOT fall back to the blind-retry loop,
    which could clobber a rival that commits during the second attempt."""
    from witan import graph as graph_mod
    from witan import server as srv

    t = server.task_create(title="twice-contended", description="x")
    real_change = srv.client.change
    calls = {"n": 0}

    def flaky_change(*args, surface_conflict=False, **kwargs):
        if surface_conflict and calls["n"] < 2:
            i = calls["n"]
            calls["n"] += 1
            if i == 1:
                # on the second attempt a rival wins before our write conflicts
                srv._update_task(
                    t["slug"],
                    {
                        "status": "in_progress",
                        "assignee": "agentB",
                        "claimed_at": srv.now_iso(),
                    },
                )
            raise graph_mod.OmnigraphConflict("stale view")
        return real_change(*args, surface_conflict=surface_conflict, **kwargs)

    monkeypatch.setattr(srv.client, "change", flaky_change)

    res = server.task_claim(t["slug"], assignee="agentA")
    assert res["claimed"] is False
    assert res["reason"] == "lost_race"
    assert res["held_by"] == "agentB"
    # both conflicts were surfaced (never fell through to a blind-retry write)
    assert calls["n"] == 2
    assert server.task_get(t["slug"])["assignee"] == "agentB"


@requires_omnigraph
def test_claim_conflict_does_not_resurrect_a_closed_task(server, monkeypatch):
    """A close committed during the CAS retry window must not be reverted back
    to in_progress by the next retry attempt. `_update_task` merges `status`
    from `claim` unconditionally regardless of what its own fresh read shows
    (see its docstring), so a retry that does not revalidate claimability
    first would silently resurrect a closed task. Review finding on the PR
    for tk-task-claim-exhausts-its-3-attempt-no-backoff-cas-674414."""
    from witan import graph as graph_mod
    from witan import server as srv

    t = server.task_create(title="closed-mid-claim", description="x")
    real_change = srv.client.change
    calls = {"n": 0}

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(srv.anyio, "sleep", no_sleep)

    def close_then_conflict(*args, surface_conflict=False, **kwargs):
        if surface_conflict and calls["n"] == 0:
            calls["n"] += 1
            # the task is closed (by a rival, or via task_close) in the
            # window between our read and our write
            srv._update_task(
                t["slug"], {"status": "closed", "closed_at": srv.now_iso()}
            )
            raise graph_mod.OmnigraphConflict("stale view")
        if surface_conflict:
            raise AssertionError("must not retry the write once the task is closed")
        return real_change(*args, surface_conflict=surface_conflict, **kwargs)

    monkeypatch.setattr(srv.client, "change", close_then_conflict)

    res = server.task_claim(t["slug"], assignee="agentA")

    assert res == {"slug": t["slug"], "claimed": False, "reason": "closed"}
    assert calls["n"] == 1
    assert server.task_get(t["slug"])["status"] == "closed"


@requires_omnigraph
def test_claim_conflict_does_not_reopen_a_blocked_task(server, monkeypatch):
    """Same regression as the closed-task case, for `blocked`."""
    from witan import graph as graph_mod
    from witan import server as srv

    t = server.task_create(title="blocked-mid-claim", description="x")
    real_change = srv.client.change
    calls = {"n": 0}

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(srv.anyio, "sleep", no_sleep)

    def block_then_conflict(*args, surface_conflict=False, **kwargs):
        if surface_conflict and calls["n"] == 0:
            calls["n"] += 1
            srv._update_task(t["slug"], {"status": "blocked"})
            raise graph_mod.OmnigraphConflict("stale view")
        if surface_conflict:
            raise AssertionError("must not retry the write once the task is blocked")
        return real_change(*args, surface_conflict=surface_conflict, **kwargs)

    monkeypatch.setattr(srv.client, "change", block_then_conflict)

    res = server.task_claim(t["slug"], assignee="agentA")

    assert res == {"slug": t["slug"], "claimed": False, "reason": "blocked"}
    assert calls["n"] == 1
    assert server.task_get(t["slug"])["status"] == "blocked"


@requires_omnigraph
def test_claim_exhausted_conflicts_report_contention_not_raise(server, monkeypatch):
    """Every retry attempt hits an OCC conflict from unrelated writes elsewhere
    on the graph — no rival ever actually holds the task. task_claim must
    exhaust its retry budget and report a structured ``{"claimed": false,
    "reason": "contention"}`` — not leak the raw `OmnigraphConflict` (the
    omnigraph "write authority ... changed during preparation" prose) to the
    caller. See tk-task-claim-exhausts-its-3-attempt-no-backoff-cas-674414."""
    from witan import graph as graph_mod
    from witan import server as srv

    t = server.task_create(title="perpetually-contended", description="x")
    calls = {"n": 0}

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(srv.anyio, "sleep", no_sleep)

    def always_conflict(*args, surface_conflict=False, **kwargs):
        if surface_conflict:
            calls["n"] += 1
            raise graph_mod.OmnigraphConflict(
                "write authority 'table_head:node:Task' changed during preparation"
            )
        raise AssertionError("unconditional write attempted mid-claim")

    monkeypatch.setattr(srv.client, "change", always_conflict)

    res = server.task_claim(t["slug"], assignee="agentA")

    assert res["claimed"] is False
    assert res["reason"] == "contention"
    assert calls["n"] == srv._CLAIM_MAX_ATTEMPTS
    # left exactly as it started, not half-claimed
    assert server.task_get(t["slug"])["status"] == "open"


@requires_omnigraph
def test_claim_retries_back_off_between_attempts(server, monkeypatch):
    """A retry after an unrelated conflict must wait, not immediately re-fire —
    three back-to-back attempts have no chance against multi-second write
    contention. See tk-task-claim-exhausts-its-3-attempt-no-backoff-cas-674414."""
    from witan import graph as graph_mod
    from witan import server as srv

    t = server.task_create(title="briefly-contended", description="x")
    real_change = srv.client.change
    calls = {"n": 0}
    sleeps = []

    async def recording_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(srv.anyio, "sleep", recording_sleep)

    def flaky_once(*args, surface_conflict=False, **kwargs):
        if surface_conflict and calls["n"] == 0:
            calls["n"] += 1
            raise graph_mod.OmnigraphConflict("stale view")
        return real_change(*args, surface_conflict=surface_conflict, **kwargs)

    monkeypatch.setattr(srv.client, "change", flaky_once)

    res = server.task_claim(t["slug"], assignee="agentA")

    assert res["claimed"] is True
    # attempt 1's backoff: base delay plus up to 10% jitter (see _claim_backoff)
    assert len(sleeps) == 1
    assert srv._CLAIM_BACKOFF_BASE <= sleeps[0] <= srv._CLAIM_BACKOFF_BASE * 1.1


# ── conditional claims (omnigraph #470 compare-and-swap) ─────────────


@requires_omnigraph
def test_claim_states_the_precondition_from_its_own_read(server, monkeypatch):
    """★ THE INVARIANT THAT MAKES THE CAS MEAN ANYTHING.

    The token must be the one `_update_task` read the merged row at — not one
    from any earlier read — because `merged` is built from THAT snapshot. A
    token from a different read fences the wrong interval, which is worse than
    no precondition at all: it looks rigorous and guarantees nothing.
    """
    from witan import server as srv

    t = server.task_create(title="conditional", description="x")
    seen = {}
    real_read = srv.client.read_with_commit
    real_change = srv.client.change

    def spy_read(*args, **kwargs):
        rows, commit = real_read(*args, **kwargs)
        # setdefault, not assignment: `task_claim` calls `read_with_commit`
        # MORE THAN ONCE — `_update_task`'s own read, and then the post-write
        # verification. Only the FIRST is the one the precondition must match,
        # and recording every call let a later one overwrite it.
        #
        # This assertion silently changed meaning when the verification read
        # was switched from `read` to `read_with_commit`: it began comparing
        # the precondition against the VERIFICATION commit, which legitimately
        # differs, and failed. The invariant under test never changed.
        seen.setdefault("read_commit", commit)
        return rows, commit

    def spy_change(*args, if_commit=None, **kwargs):
        seen.setdefault("if_commit", if_commit)
        return real_change(*args, **kwargs)

    monkeypatch.setattr(srv.client, "read_with_commit", spy_read)
    monkeypatch.setattr(srv.client, "change", spy_change)

    server.task_claim(t["slug"], assignee="agentA")

    assert "if_commit" in seen, "the claim write stated no precondition at all"
    assert seen["if_commit"] == seen["read_commit"]


@requires_omnigraph
def test_claim_still_works_when_the_tier_supplies_no_commit(server, monkeypatch):
    """The degraded path must stay a working claim, not an exception.

    A pre-#470 server (and the CLI path) returns no `graph_commit_id`. That is a
    supported state — it is exactly today's best-effort claim, with the
    post-write verification as the backstop — so it must not raise, and it must
    not send a precondition it does not have.
    """
    from witan import server as srv

    t = server.task_create(title="no-commit-tier", description="x")
    real_read = srv.client.read_with_commit
    real_change = srv.client.change
    sent = {}

    def read_without_commit(*args, **kwargs):
        rows, _ = real_read(*args, **kwargs)
        return rows, None

    def spy_change(*args, if_commit=None, **kwargs):
        sent.setdefault("if_commit", if_commit)
        return real_change(*args, **kwargs)

    monkeypatch.setattr(srv.client, "read_with_commit", read_without_commit)
    monkeypatch.setattr(srv.client, "change", spy_change)

    res = server.task_claim(t["slug"], assignee="agentA")

    assert res["claimed"] is True
    assert sent["if_commit"] is None


async def _instant_sleep(_seconds):
    """Stand-in for ``anyio.sleep`` in the catch-up-retry tests below — the
    loop's correctness is about attempt COUNT and comparison, not real time,
    and there is no reason a unit test should actually wait out the backoff."""


@requires_omnigraph
def test_claim_verification_retries_until_caught_up(server, monkeypatch):
    """★ THE CATCH-UP LOOP, THE ACTUAL FIX FOR
    tk-mutual-exclusion-violated-2-of-8-racers-both-got-52b3dd. The proven bug
    was a verification read reporting a genuinely OLDER commit than a write
    that had already landed — not a lie about its own content, an honestly
    stale read. A read reporting an older commit than our own write must be
    retried, not trusted, until it reports one that is at least as new.

    An earlier version of this fix PINNED the verification read to our own
    write's commit instead of retrying an unconstrained one — review (Copilot,
    agent-kit#248) caught that a pinned read can never see a later write from
    someone else, defeating `test_claim_post_write_verification_catches_last_
    writer` on any transport where the pin actually engages. This test is the
    replacement design's regression guard: unconstrained, but not trusted
    until caught up.
    """
    from witan import server as srv

    t = server.task_create(title="catch-up", description="x")
    real_read_with_commit = srv.client.read_with_commit
    real_change = srv.client.change

    def fake_change(*args, **kwargs):
        # The real write still happens — this only substitutes the returned
        # commit id, so the row content read back afterwards is genuine.
        real_change(*args, **kwargs)
        return "01WRITE"

    calls = {"n": 0}
    verify_calls = {"n": 0}

    def staggered_read(*args, **kwargs):
        rows, real_commit = real_read_with_commit(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            # `_update_task`'s own precondition read — must stay real, or the
            # write's CAS precondition would be fenced against a fabricated
            # commit the real store never had.
            return rows, real_commit
        verify_calls["n"] += 1
        if verify_calls["n"] < 3:
            return rows, "00STALE"
        return rows, "01WRITE"

    monkeypatch.setattr(srv.client, "change", fake_change)
    monkeypatch.setattr(srv.client, "read_with_commit", staggered_read)
    monkeypatch.setattr(srv.anyio, "sleep", _instant_sleep)

    res = server.task_claim(t["slug"], assignee="agentA")

    assert res["claimed"] is True
    assert verify_calls["n"] == 3, "must retry past the two stale reads"


@requires_omnigraph
def test_claim_verification_gives_up_after_max_attempts(server, monkeypatch):
    """A verification read that never catches up must not retry forever — the
    loop is bounded and exits, trusting whatever it last saw, rather than
    hanging the claim on a genuinely wedged read path."""
    from witan import server as srv

    t = server.task_create(title="never-catches-up", description="x")
    real_read_with_commit = srv.client.read_with_commit
    real_change = srv.client.change

    def fake_change(*args, **kwargs):
        real_change(*args, **kwargs)
        return "01WRITE"

    calls = {"n": 0}

    def always_stale_read(*args, **kwargs):
        rows, real_commit = real_read_with_commit(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            return rows, real_commit
        return rows, "00STALE"

    monkeypatch.setattr(srv.client, "change", fake_change)
    monkeypatch.setattr(srv.client, "read_with_commit", always_stale_read)
    monkeypatch.setattr(srv.anyio, "sleep", _instant_sleep)

    res = server.task_claim(t["slug"], assignee="agentA")

    # 1 for `_update_task`'s own read, plus every bounded verify attempt.
    assert calls["n"] == 1 + srv._VERIFY_CAUGHT_UP_MAX_ATTEMPTS
    # No rival ever wrote, so even the still-stale last read shows agentA —
    # the point here is termination, not this particular outcome.
    assert res["claimed"] is True


def _verify_events(capfd):
    """The ``witan.task_claim.verify`` payloads the server emitted on stderr.

    Read off the real JSON log stream rather than through ``caplog``, which
    does not see these once ``configure_logging(force=True)`` has
    reconfigured logging. Parsing the JSON the server actually writes is the
    stronger assertion anyway: it pins the shape production emits.
    """
    import json

    events = []
    for line in capfd.readouterr().err.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("event") == "witan.task_claim.verify":
            events.append(payload)
    return events


@requires_omnigraph
def test_verify_log_distinguishes_a_checked_catch_up_from_a_skipped_one(
    server, monkeypatch, capfd
):
    """``caught_up`` alone cannot tell the healthy case from the degraded one.

    It is ``True`` both when the catch-up check ran and passed and when
    ``write_commit is None`` short-circuited it, and ``verify_attempts`` is 1
    in both — so a log carrying only those two fields reads as "no staleness"
    on a tier that never supplied a commit to catch up to. Investigating the
    deployed service against exactly these fields is what surfaced the gap:
    the healthy reading had to be argued from a *neighbouring* call's
    ``witan.task_update.conditional`` line rather than read off this one.

    ``write_graph_commit_id`` is what settles it, so both directions are
    pinned here rather than only the happy one. The commit-supplying tier is
    simulated because the CLI transport these tests run over never returns a
    ``graph_commit_id`` — the same reason the neighbouring catch-up tests
    stub ``change``.
    """
    from witan_core.observability import configure_logging, reset_logging

    from witan import server as srv

    real_change = srv.client.change

    # Sorts BEFORE a real ULID (which currently starts "01M..."), so the
    # catch-up comparison is satisfied on the first read. A sentinel sorting
    # after one would exhaust the retry loop and report caught_up=False —
    # correctly, which is the ordering these ids are chosen for.
    def change_returning_a_commit(*args, **kwargs):
        real_change(*args, **kwargs)
        return "00WRITE"

    configure_logging(log_format="json", level="INFO", force=True)
    try:
        t = server.task_create(title="commit-supplying-tier", description="x")
        monkeypatch.setattr(srv.client, "change", change_returning_a_commit)
        capfd.readouterr()
        server.task_claim(t["slug"], assignee="agentA")
        [supplied] = _verify_events(capfd)

        monkeypatch.setattr(srv.client, "change", real_change)
        t2 = server.task_create(title="no-commit-tier", description="x")
        capfd.readouterr()
        server.task_claim(t2["slug"], assignee="agentA")
        [degraded] = _verify_events(capfd)
    finally:
        reset_logging()

    # Both look identical on the two fields that used to be the whole story.
    assert supplied["caught_up"] is degraded["caught_up"] is True
    assert supplied["verify_attempts"] == degraded["verify_attempts"] == 1
    # The new field is the only thing that separates them.
    assert supplied["write_graph_commit_id"] == "00WRITE"
    assert degraded["write_graph_commit_id"] is None


@requires_omnigraph
def test_conditional_update_refuses_to_ride_with_extra_steps(server):
    """Refused, not silently downgraded to an unconditional multi-step commit.

    A caller asking for a precondition and not getting one is the failure mode
    this whole change exists to remove, so the unsupported combination is an
    error rather than a quiet weakening.
    """
    from witan import server as srv

    t = server.task_create(title="batched", description="x")
    with pytest.raises(ValueError, match="extra_steps"):
        srv._update_task(
            t["slug"],
            {"status": "in_progress"},
            conditional=True,
            extra_steps=[("mutations.gq", "update_task", {"slug": t["slug"]})],
        )
