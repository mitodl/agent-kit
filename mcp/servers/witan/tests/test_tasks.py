"""End-to-end tests for the dependency-aware task tracker."""

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
    from witan import server as srv

    # Make any claim's lease count as elapsed immediately.
    monkeypatch.setattr(srv, "_CLAIM_LEASE_SECONDS", -1)

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
                    "claimed_at": srv._now_iso(),
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
                    "claimed_at": srv._now_iso(),
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
                        "claimed_at": srv._now_iso(),
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
