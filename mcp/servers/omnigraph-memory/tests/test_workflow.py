"""End-to-end tests for workflow project/session/trace tracking."""

import uuid

from .conftest import requires_omnigraph


@requires_omnigraph
def test_project_lifecycle_and_trace(server):
    proj = server.workflow_project_create(
        title="ship feature",
        description="build it",
        phase="discovery",
        github_issue="https://github.com/test/repo/issues/9",
    )
    assert proj["slug"].startswith("wp-")

    advanced = server.workflow_project_advance(proj["slug"], phase="implementation")
    assert advanced["phase"] == "implementation"

    sid = uuid.uuid4().hex
    sess = server.workflow_session_start(
        project_slug=proj["slug"],
        session_id=sid,
        phase="implementation",
    )
    assert sess["session_slug"].startswith("ws-")

    server.workflow_session_end(
        sess["session_slug"],
        summary="did the work",
        tools_used=["Edit", "Bash"],
        files_changed=["a.py"],
    )

    done = server.workflow_project_complete(proj["slug"], outcome="delivered")
    assert done["existed"] is False
    assert done["trace_slug"] == f"wt-{proj['slug']}"

    # idempotent: completing again returns the existing trace
    again = server.workflow_project_complete(proj["slug"], outcome="delivered")
    assert again["existed"] is True


@requires_omnigraph
def test_link_memory_to_project(server):
    proj = server.workflow_project_create(title="linked", description="d")
    mem = server.memory_store(kind="lesson", title="watch out", content="be careful")
    res = server.workflow_project_link_memory(proj["slug"], mem["slug"])
    assert res["project_slug"] == proj["slug"]
    assert res["memory_slug"] == mem["slug"]


@requires_omnigraph
def test_list_across_all_repos_with_empty_repo(server):
    p1 = server.workflow_project_create(
        title="A", description="d", repo="https://github.com/x/one"
    )
    p2 = server.workflow_project_create(
        title="B", description="d", repo="https://github.com/x/two"
    )
    all_active = {p["slug"] for p in server.workflow_project_list(repo="")}
    assert {p1["slug"], p2["slug"]} <= all_active


@requires_omnigraph
def test_project_list_active_default(server):
    proj = server.workflow_project_create(title="active one", description="d")
    active = {p["slug"] for p in server.workflow_project_list()}
    assert proj["slug"] in active

    server.workflow_project_complete(proj["slug"], outcome="done")
    active_after = {p["slug"] for p in server.workflow_project_list()}
    assert proj["slug"] not in active_after
    completed = {p["slug"] for p in server.workflow_project_list(status="completed")}
    assert proj["slug"] in completed
