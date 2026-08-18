"""End-to-end tests for the dependency-aware task tracker."""

import pytest

from .conftest import requires_omnigraph


@requires_omnigraph
def test_create_defaults(server):
    res = server.task_create(title="do a thing", description="x")
    assert res["slug"].startswith("tk-")
    assert res["status"] == "open"
    node = server.task_get(res["slug"])
    assert node["type"] == "task"
    assert node["priority"] == "p2"


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
