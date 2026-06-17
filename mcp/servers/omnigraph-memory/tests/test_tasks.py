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
