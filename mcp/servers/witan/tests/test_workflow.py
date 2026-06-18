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
        title="A", description="d", repos=["https://github.com/x/one"]
    )
    p2 = server.workflow_project_create(
        title="B", description="d", repos=["https://github.com/x/two"]
    )
    all_active = {p["slug"] for p in server.workflow_project_list(repo="")}
    assert {p1["slug"], p2["slug"]} <= all_active


@requires_omnigraph
def test_multi_repo_membership(server):
    r1, r2 = "https://github.com/x/one", "https://github.com/x/two"
    proj = server.workflow_project_create(
        title="multi", description="d", repos=[r1, r2]
    )
    assert {r1, r2} <= set(proj["repos"])

    # discoverable from either member repo, not from an unrelated one
    assert proj["slug"] in {p["slug"] for p in server.workflow_project_list(repo=r1)}
    assert proj["slug"] in {p["slug"] for p in server.workflow_project_list(repo=r2)}
    others = {
        p["slug"]
        for p in server.workflow_project_list(repo="https://github.com/x/nope")
    }
    assert proj["slug"] not in others


@requires_omnigraph
def test_repo_set_accretes_from_session(server):
    r1, r2 = "https://github.com/x/alpha", "https://github.com/x/beta"
    proj = server.workflow_project_create(title="accrete", description="d", repos=[r1])
    assert proj["slug"] not in {
        p["slug"] for p in server.workflow_project_list(repo=r2)
    }

    server.workflow_session_start(
        project_slug=proj["slug"],
        session_id=uuid.uuid4().hex,
        phase="discovery",
        repo=r2,
    )

    # the session's repo is now part of the project's set
    got = server.workflow_project_get(proj["slug"])
    assert {r1, r2} <= set(got["repos"])
    assert proj["slug"] in {p["slug"] for p in server.workflow_project_list(repo=r2)}


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
