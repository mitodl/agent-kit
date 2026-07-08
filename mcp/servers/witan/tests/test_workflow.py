"""End-to-end tests for workflow project/session/trace tracking."""

import uuid

import pytest

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

    # workflow_trace_get resolves by either the wt- trace slug or the wp- slug.
    by_wt = server.workflow_trace_get(f"wt-{proj['slug']}")
    by_wp = server.workflow_trace_get(proj["slug"])
    assert by_wt is not None
    assert by_wt == by_wp
    assert by_wt["outcome"] == "delivered"
    assert by_wt["session_count"] == 1
    assert "implementation" in (by_wt.get("phases") or [])


@requires_omnigraph
def test_trace_get_missing_returns_none(server):
    # A project with no completion has no trace yet.
    proj = server.workflow_project_create(title="incomplete", description="d")
    assert server.workflow_trace_get(proj["slug"]) is None
    assert server.workflow_trace_get("wt-does-not-exist") is None


@requires_omnigraph
def test_project_status_resume_view(server):
    proj = server.workflow_project_create(
        title="resume me", description="d", phase="implementation"
    )
    slug = proj["slug"]

    # A ready task and a closed one under the project.
    ready = server.task_create(
        title="do the thing", description="x", project_slug=slug, priority="p1"
    )
    done = server.task_create(title="already done", description="x", project_slug=slug)
    server.task_close(done["slug"])

    # Two sessions; the later-started one carries the handoff summary.
    server.workflow_session_start(
        project_slug=slug, session_id=uuid.uuid4().hex, phase="spec"
    )
    s2 = server.workflow_session_start(
        project_slug=slug, session_id=uuid.uuid4().hex, phase="implementation"
    )
    server.workflow_session_end(s2["session_slug"], summary="wired the helper")

    st = server.workflow_project_status(slug)
    assert st["project"]["phase"] == "implementation"
    assert st["counts"] == {"ready": 1, "open_tasks": 1}
    assert [t["slug"] for t in st["ready_tasks"]] == [ready["slug"]]
    assert st["last_session"]["summary"] == "wired the helper"
    assert st["last_session"]["open"] is False
    assert st["blockers"] == []


@requires_omnigraph
def test_project_status_missing_returns_none(server):
    assert server.workflow_project_status("wp-does-not-exist") is None


@requires_omnigraph
def test_project_status_respects_external_blocker(server):
    # A task blocked by one OUTSIDE the project must not show as ready — the
    # status view delegates to task_ready, which fetches the external blocker
    # rather than treating an out-of-project blocker as closed.
    proj = server.workflow_project_create(title="ext blocker", description="d")
    gate = server.task_create(title="external gate", description="x")  # unscoped, open
    blocked = server.task_create(
        title="waits on external",
        description="x",
        project_slug=proj["slug"],
        blocked_by=[gate["slug"]],
    )
    st = server.workflow_project_status(proj["slug"])
    assert st["ready_tasks"] == []
    assert st["counts"]["ready"] == 0

    server.task_close(gate["slug"])
    st2 = server.workflow_project_status(proj["slug"])
    assert [t["slug"] for t in st2["ready_tasks"]] == [blocked["slug"]]


@requires_omnigraph
def test_project_status_no_sessions(server):
    proj = server.workflow_project_create(title="fresh", description="d")
    st = server.workflow_project_status(proj["slug"])
    assert st["last_session"] is None


@requires_omnigraph
def test_advance_advisory_on_unusual_transitions(server):
    p = server.workflow_project_create(title="adv", description="d", phase="discovery")
    # normal forward step → no advisory
    assert "advisory" not in server.workflow_project_advance(p["slug"], phase="spec")
    # skip ahead spec → delivery (bypasses implementation)
    skip = server.workflow_project_advance(p["slug"], phase="delivery")
    assert "advisory" in skip and "Skipped ahead" in skip["advisory"]
    assert "implementation" in skip["advisory"]
    # backward delivery → discovery
    back = server.workflow_project_advance(p["slug"], phase="discovery")
    assert "advisory" in back and "backward" in back["advisory"].lower()
    # no-op re-advance to the same phase
    noop = server.workflow_project_advance(p["slug"], phase="discovery")
    assert "advisory" in noop and "no change" in noop["advisory"].lower()


@requires_omnigraph
def test_memory_store_flags_missing_session(server):
    r = server.memory_store(kind="lesson", title="t", content="c")
    assert r["session_linked"] is False
    assert "workflow_session_start" in r.get("note", "")


@requires_omnigraph
def test_memory_store_links_active_session(server, monkeypatch):
    proj = server.workflow_project_create(title="p", description="d")
    sid = "test-session-b2"
    monkeypatch.setenv("CLAUDE_SESSION_ID", sid)
    server.workflow_session_start(
        project_slug=proj["slug"], session_id=sid, phase="spec"
    )
    r = server.memory_store(kind="lesson", title="t", content="c")
    assert r["session_linked"] is True
    assert "note" not in r


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


# ── Project dependency / blocking tests ──────────────────────────────────────


@requires_omnigraph
def test_project_block_and_unblock(server):
    """Blocking sets blocked_by on the downstream project; unblocking removes it."""
    upstream = server.workflow_project_create(title="upstream", description="d")
    downstream = server.workflow_project_create(title="downstream", description="d")

    res = server.workflow_project_block(upstream["slug"], downstream["slug"])
    assert res["blocker"] == upstream["slug"]
    assert res["blocked"] == downstream["slug"]
    assert res["linked"] is True  # success flag present on both paths (C8)

    # self-block soft-fails with linked=False rather than raising (C8).
    selfblock = server.workflow_project_block(upstream["slug"], upstream["slug"])
    assert selfblock["linked"] is False
    assert "reason" in selfblock

    got = server.workflow_project_get(downstream["slug"])
    assert upstream["slug"] in (got.get("blocked_by") or [])

    # blocker appears in the upstream project's `blocks` list
    up_got = server.workflow_project_get(upstream["slug"])
    assert downstream["slug"] in (up_got.get("blocks") or [])

    # unblock removes the entry
    server.workflow_project_unblock(upstream["slug"], downstream["slug"])
    after = server.workflow_project_get(downstream["slug"])
    assert upstream["slug"] not in (after.get("blocked_by") or [])


@requires_omnigraph
def test_get_project_blockers(server):
    """workflow_project_get_blockers returns full blocker nodes."""
    blocker1 = server.workflow_project_create(title="blocker one", description="d")
    blocker2 = server.workflow_project_create(title="blocker two", description="d")
    blocked = server.workflow_project_create(title="blocked project", description="d")

    server.workflow_project_block(blocker1["slug"], blocked["slug"])
    server.workflow_project_block(blocker2["slug"], blocked["slug"])

    blockers = server.workflow_project_get_blockers(blocked["slug"])
    blocker_slugs = {b["slug"] for b in blockers}
    assert {blocker1["slug"], blocker2["slug"]} == blocker_slugs


@requires_omnigraph
def test_blocked_project_not_ready(server):
    """A project whose blocker is still active does not appear in ready list."""
    upstream = server.workflow_project_create(
        title="must finish first", description="d"
    )
    downstream = server.workflow_project_create(
        title="waits for upstream", description="d"
    )

    server.workflow_project_block(upstream["slug"], downstream["slug"])

    ready = {p["slug"] for p in server.workflow_project_list(ready=True, repo="")}
    # upstream is ready (no blockers), downstream is not
    assert upstream["slug"] in ready
    assert downstream["slug"] not in ready

    # complete the upstream; now downstream becomes ready
    server.workflow_project_complete(upstream["slug"], outcome="shipped")
    ready_after = {p["slug"] for p in server.workflow_project_list(ready=True, repo="")}
    assert downstream["slug"] in ready_after


@requires_omnigraph
def test_blocking_completed_project_does_not_block(server):
    """If the blocker is already completed the downstream project is still ready."""
    upstream = server.workflow_project_create(title="already done", description="d")
    downstream = server.workflow_project_create(title="free to go", description="d")

    server.workflow_project_complete(upstream["slug"], outcome="done")
    server.workflow_project_block(upstream["slug"], downstream["slug"])

    ready = {p["slug"] for p in server.workflow_project_list(ready=True, repo="")}
    assert downstream["slug"] in ready


@requires_omnigraph
def test_list_blocked_projects_includes_blocked_by(server):
    """list_projects_by_status returns blocked_by field."""
    blocker = server.workflow_project_create(title="a blocker", description="d")
    blocked = server.workflow_project_create(title="a blocked", description="d")

    server.workflow_project_block(blocker["slug"], blocked["slug"])

    rows = server.workflow_project_list(status="active", repo="")
    blocked_row = next((r for r in rows if r["slug"] == blocked["slug"]), None)
    assert blocked_row is not None
    assert blocker["slug"] in (blocked_row.get("blocked_by") or [])


@requires_omnigraph
def test_trace_list_filters_by_repo_tags_author(server):
    proj = server.workflow_project_create(
        title="traced project",
        description="d",
        repos=["https://github.com/x/one"],
        tags=["alpha"],
    )
    server.workflow_project_complete(proj["slug"], outcome="done")

    other = server.workflow_project_create(
        title="other project", description="d", repos=["https://github.com/x/two"]
    )
    server.workflow_project_complete(other["slug"], outcome="done")

    by_repo = server.workflow_trace_list(repo="https://github.com/x/one")
    assert {t["slug"] for t in by_repo} == {f"wt-{proj['slug']}"}

    by_tag = server.workflow_trace_list(repo="", tags=["alpha"])
    assert {t["slug"] for t in by_tag} == {f"wt-{proj['slug']}"}

    by_author = server.workflow_trace_list(repo="", author="nobody")
    assert by_author == []


@requires_omnigraph
def test_trace_list_tags_as_bare_string_is_coerced(server):
    """A single string (an easy LLM-caller mistake) must not be iterated char-by-char."""
    proj = server.workflow_project_create(
        title="stringy tags", description="d", tags=["alpha"]
    )
    server.workflow_project_complete(proj["slug"], outcome="done")

    rows = server.workflow_trace_list(repo="", tags="alpha")
    assert {t["slug"] for t in rows} == {f"wt-{proj['slug']}"}


@requires_omnigraph
def test_trace_annotate_unions_and_is_idempotent(server):
    proj = server.workflow_project_create(title="annotate me", description="d")
    done = server.workflow_project_complete(proj["slug"], outcome="done")
    trace_slug = done["trace_slug"]

    first = server.workflow_trace_annotate(trace_slug, lessons_slug=["les-a"])
    assert first["lessons_slug"] == ["les-a"]

    second = server.workflow_trace_annotate(
        trace_slug, lessons_slug=["les-a", "les-b"], patterns_slug=["pat-a"]
    )
    assert second["lessons_slug"] == ["les-a", "les-b"]
    assert second["patterns_slug"] == ["pat-a"]


@requires_omnigraph
def test_trace_annotate_bare_string_slugs_are_coerced(server):
    proj = server.workflow_project_create(title="stringy annotate", description="d")
    done = server.workflow_project_complete(proj["slug"], outcome="done")

    result = server.workflow_trace_annotate(
        done["trace_slug"], lessons_slug="les-a", patterns_slug="pat-a"
    )
    assert result["lessons_slug"] == ["les-a"]
    assert result["patterns_slug"] == ["pat-a"]


@requires_omnigraph
def test_mine_trace_without_proposals_returns_material(server):
    proj = server.workflow_project_create(title="mine me", description="d")
    sid = uuid.uuid4().hex
    sess = server.workflow_session_start(
        project_slug=proj["slug"], session_id=sid, phase="discovery"
    )
    server.workflow_session_end(sess["session_slug"], summary="learned a thing")
    done = server.workflow_project_complete(proj["slug"], outcome="shipped it")

    material = server.workflow_trace_mine(done["trace_slug"])
    assert material["trace"]["slug"] == done["trace_slug"]
    assert [s["slug"] for s in material["sessions"]] == [sess["session_slug"]]


@requires_omnigraph
def test_mine_trace_with_proposals_creates_memories_and_links_project(server):
    proj = server.workflow_project_create(title="mine me too", description="d")
    done = server.workflow_project_complete(proj["slug"], outcome="shipped it")
    trace_slug = done["trace_slug"]

    created = server.workflow_trace_mine(
        trace_slug,
        patterns=[{"title": "a pattern", "content": "do X because Y"}],
        lessons=[{"title": "a lesson", "content": "watch out for Z"}],
    )
    assert len(created["created_patterns"]) == 1
    assert len(created["created_lessons"]) == 1

    trace = server.workflow_trace_list(repo="")
    tr = next(t for t in trace if t["slug"] == trace_slug)
    assert tr["patterns_slug"] == created["created_patterns"]
    assert tr["lessons_slug"] == created["created_lessons"]

    informed = {
        m["slug"] for m in server.workflow_project_memories(proj["slug"])["memories"]
    }
    assert informed == {*created["created_patterns"], *created["created_lessons"]}


@requires_omnigraph
def test_mine_trace_bare_dict_proposal_is_coerced(server):
    """A single dict (not wrapped in a list) is a common LLM-caller mistake."""
    proj = server.workflow_project_create(title="mine bare dict", description="d")
    done = server.workflow_project_complete(proj["slug"], outcome="shipped it")

    created = server.workflow_trace_mine(
        done["trace_slug"],
        patterns={"title": "a pattern", "content": "do X because Y"},
    )
    assert len(created["created_patterns"]) == 1
    assert created["created_lessons"] == []


@requires_omnigraph
def test_mine_trace_rejects_proposal_missing_required_keys(server):
    proj = server.workflow_project_create(title="mine invalid", description="d")
    done = server.workflow_project_complete(proj["slug"], outcome="shipped it")

    with pytest.raises(ValueError, match="title.*content"):
        server.workflow_trace_mine(
            done["trace_slug"], patterns=[{"title": "no content"}]
        )


@requires_omnigraph
def test_missing_trace_returns_consistent_shape(server):
    """Both trace tools return {"slug": ..., "error": ...} for a missing trace (C8)."""
    mined = server.workflow_trace_mine("wt-does-not-exist")
    annotated = server.workflow_trace_annotate(
        "wt-does-not-exist", lessons_slug=["les-x"]
    )
    for res in (mined, annotated):
        assert res["slug"] == "wt-does-not-exist"
        assert res["error"] == "no such trace"
