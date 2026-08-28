"""End-to-end tests for workflow project/session/trace tracking."""

import uuid
from datetime import datetime

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
def test_memory_store_links_active_session(server, tmp_state_dir, monkeypatch):
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


# ── Project search (tk-phase-0-bm25-task-search-project-search) ─────────────


@requires_omnigraph
def test_project_search_bm25_ranked(server):
    server.workflow_project_create(
        title="vault k8s auth", description="wire vault kubernetes auth for a service"
    )
    server.workflow_project_create(
        title="unrelated", description="something about a totally different area"
    )

    hits = server.workflow_project_search("vault kubernetes auth")
    assert hits and hits[0]["title"] == "vault k8s auth"


@requires_omnigraph
def test_project_search_finds_title_only_terms(server):
    proj = server.workflow_project_create(
        title="zebrafish quokka narwhal",
        description="totally unrelated prose about compaction and fragments",
    )

    hits = server.workflow_project_search("zebrafish quokka narwhal")
    assert [h["slug"] for h in hits] == [proj["slug"]]


@requires_omnigraph
def test_project_search_dedups_both_field_matches(server):
    proj = server.workflow_project_create(
        title="quokka narwhal", description="more about the quokka narwhal"
    )

    hits = server.workflow_project_search("quokka narwhal")
    assert [h["slug"] for h in hits].count(proj["slug"]) == 1


@requires_omnigraph
def test_project_search_excludes_completed_by_default(server):
    proj = server.workflow_project_create(
        title="quokka narwhal project", description="quokka narwhal work"
    )
    server.workflow_project_complete(proj["slug"], outcome="done")

    assert server.workflow_project_search("quokka narwhal") == []
    all_status = server.workflow_project_search("quokka narwhal", status=None)
    assert proj["slug"] in [h["slug"] for h in all_status]


@requires_omnigraph
def test_project_create_returns_similar_projects(server):
    existing = server.workflow_project_create(
        title="dedup graph search", description="add semantic search to the graph"
    )

    created = server.workflow_project_create(
        title="dedup graph search v2", description="add semantic search to the graph"
    )
    assert existing["slug"] in [s["slug"] for s in created["similar"]]


@requires_omnigraph
def test_project_create_similar_empty_when_no_matches(server):
    created = server.workflow_project_create(
        title="zzz nothing else like this zzz", description="wholly novel objective"
    )
    assert created["similar"] == []


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


# ── Session handle (MCP 2026-07-28 stateless core) ───────────────────────────
#
# The protocol carries no session state and a deployment round-robins across
# replicas, so the tool-returned handle is the only thing tying
# workflow_session_start to workflow_session_end.


@requires_omnigraph
def test_session_start_returns_an_explicit_handle(server, tmp_state_dir):
    from witan import server as srv

    proj = server.workflow_project_create(title="handle", description="d")
    sid = uuid.uuid4().hex
    handle = server.workflow_session_start(
        project_slug=proj["slug"], session_id=sid, phase="spec"
    )

    # Everything the caller needs to close the session later, with no reliance
    # on transport state or on reaching the same replica twice.
    assert handle["session_slug"].startswith("ws-")
    assert handle["project_slug"] == proj["slug"]
    assert handle["phase"] == "spec"
    assert handle["session_id"] == sid
    assert handle["started_at"]
    assert srv._is_local_stdio()


@requires_omnigraph
def test_local_stdio_server_parks_the_handle_for_the_stop_hook(server, tmp_state_dir):
    from witan import session_state

    proj = server.workflow_project_create(title="parked", description="d")
    sid = uuid.uuid4().hex
    handle = server.workflow_session_start(
        project_slug=proj["slug"], session_id=sid, phase="spec"
    )

    assert session_state.read_handle(sid) == handle

    server.workflow_session_end(handle["session_slug"], summary="done")
    assert session_state.read_handle(sid) is None


@requires_omnigraph
def test_deployed_server_does_not_write_a_handle(server, tmp_state_dir, monkeypatch):
    """A replica behind a load balancer shares no filesystem with the agent's
    Stop hook, so a server-written handle is useless at best and a stale
    pointer at worst. The client persists the returned handle instead."""
    from witan import server as srv
    from witan import session_state

    monkeypatch.setattr(
        srv,
        "identity_cfg",
        srv.identity_cfg.model_copy(
            update={"oidc_issuer": "https://sso.example.org/realms/ol"}
        ),
    )
    assert not srv._is_local_stdio()

    proj = server.workflow_project_create(title="deployed", description="d")
    sid = uuid.uuid4().hex
    handle = server.workflow_session_start(
        project_slug=proj["slug"], session_id=sid, phase="spec"
    )

    # The session is still created and the handle still returned — only the
    # filesystem side effect is gone.
    assert handle["session_slug"].startswith("ws-")
    assert session_state.read_handle(sid) is None


@requires_omnigraph
def test_stop_hook_closes_the_session_via_the_handle(
    server, tmp_state_dir, no_background_optimize, monkeypatch
):
    """End-to-end: start → park handle → Stop hook reads it back and closes."""
    from witan import session_state
    from witan.cli import _common, hooks

    proj = server.workflow_project_create(title="hooked", description="d")
    sid = uuid.uuid4().hex
    handle = server.workflow_session_start(
        project_slug=proj["slug"], session_id=sid, phase="spec"
    )
    monkeypatch.setenv("CLAUDE_SESSION_ID", sid)

    # The hook dispatches through _srv(); point it at the test-wired server
    # module so the close lands in this test's throwaway store.
    from witan import server as srv

    monkeypatch.setattr(_common, "_server", srv)

    hooks.session_checkpoint()

    sessions = srv.client.read(
        "read.gq", "list_sessions_by_project", {"project_slug": proj["slug"]}
    )
    closed = [s for s in sessions if s["slug"] == handle["session_slug"]]
    assert closed and closed[0]["ended_at"]
    assert "auto-closed by Stop hook" in closed[0]["summary"]
    # Handle consumed, so a second Stop can't re-close it.
    assert session_state.read_handle(sid) is None


@requires_omnigraph
def test_stop_hook_is_a_noop_without_a_handle(
    server, tmp_state_dir, no_background_optimize, monkeypatch
):
    from witan.cli import hooks

    monkeypatch.setenv("CLAUDE_SESSION_ID", uuid.uuid4().hex)
    hooks.session_checkpoint()  # must not raise


@pytest.mark.parametrize(
    "boom",
    [
        RuntimeError("offline"),
        SystemExit(1),  # _srv() raises this for a half-configured remote
    ],
)
def test_stop_hook_keeps_the_handle_when_the_close_fails(
    boom, tmp_state_dir, no_background_optimize, monkeypatch
):
    """A failed close must not discard the handle.

    The close now goes over the network, so failure is usually transient — an
    expired token or an offline laptop. Dropping the handle there would throw
    away the only pointer to the session and leak it open in the graph forever.
    """
    from witan import session_state
    from witan.cli import _common, hooks

    sid = uuid.uuid4().hex
    session_state.write_handle(sid, {"session_slug": "ws-doomed"})
    monkeypatch.setenv("CLAUDE_SESSION_ID", sid)

    class _Exploding:
        def __getattr__(self, _name):
            def _raise(*_a, **_kw):
                raise boom

            return _raise

    monkeypatch.setattr(_common, "_server", _Exploding())

    hooks.session_checkpoint()  # never blocks the agent, not even on SystemExit

    assert session_state.read_handle(sid) == {"session_slug": "ws-doomed"}


def test_stop_hook_survives_a_broken_config(tmp_state_dir, monkeypatch):
    """`cfg_module.load()` raises ValueError on a malformed config.toml or an
    unknown [targets.*] selection. The Stop hook is documented as always exiting
    0 and never blocking, so a broken config must not turn into a failed stop."""
    from witan.cli import hooks

    monkeypatch.setenv("CLAUDE_SESSION_ID", uuid.uuid4().hex)

    def _boom(*_a, **_kw):
        raise ValueError("The 'rank' section in config must be a table.")

    monkeypatch.setattr(hooks.cfg_module, "load", _boom)

    hooks.session_checkpoint()  # must not raise


# ── workflow_session_start idempotency ───────────────────────────────────────
#
# A hook retry, transport reconnect or replica failover re-fires the call. It
# used to mint a second node every time; now it returns the open one.


@requires_omnigraph
def test_session_start_is_idempotent_while_the_session_is_open(server, tmp_state_dir):
    proj = server.workflow_project_create(title="retry", description="d")
    sid = uuid.uuid4().hex

    first = server.workflow_session_start(
        project_slug=proj["slug"], session_id=sid, phase="implementation"
    )
    second = server.workflow_session_start(
        project_slug=proj["slug"], session_id=sid, phase="implementation"
    )

    assert first["existed"] is False
    assert second["existed"] is True
    assert second["session_slug"] == first["session_slug"]

    sessions = server.workflow_project_status(proj["slug"])
    assert sessions["last_session"]["slug"] == first["session_slug"]

    # One node, not two — the whole point.
    from witan import server as srv

    rows = srv.client.read(
        "read.gq", "sessions_for_key", {"project_slug": proj["slug"], "session_id": sid}
    )
    assert len(rows) == 1
    # The retry reports the session's real start, not its own arrival time. It
    # comes back as the store spells it, so compare against the stored row.
    assert second["started_at"] == rows[0]["started_at"]
    assert datetime.fromisoformat(second["started_at"]) <= datetime.fromisoformat(
        first["started_at"]
    )


@requires_omnigraph
def test_session_start_merges_repo_and_tags_on_retry(server, tmp_state_dir):
    """The repo-adding workaround still works, without the duplicate node it used
    to leave behind: a second call's repo lands on the project's set, and the
    session keeps the repo/tags the first call recorded."""
    r1, r2 = "https://github.com/x/one", "https://github.com/x/two"
    proj = server.workflow_project_create(title="merge repos", description="d")
    sid = uuid.uuid4().hex

    first = server.workflow_session_start(
        project_slug=proj["slug"],
        session_id=sid,
        phase="spec",
        repo=r1,
        tags=["a"],
    )
    second = server.workflow_session_start(
        project_slug=proj["slug"],
        session_id=sid,
        phase="spec",
        repo=r2,
        tags=["b"],
    )
    assert second["session_slug"] == first["session_slug"]

    # Both repos reached the project even though only one session exists.
    assert {r1, r2} <= set(server.workflow_project_get(proj["slug"])["repos"])

    from witan import server as srv

    rows = srv.client.read(
        "read.gq", "sessions_for_key", {"project_slug": proj["slug"], "session_id": sid}
    )
    assert len(rows) == 1
    assert set(rows[0]["tags"]) == {"a", "b"}


@requires_omnigraph
def test_session_start_after_end_starts_a_new_session(server, tmp_state_dir):
    """One $CLAUDE_SESSION_ID legitimately spans several working stints. Once a
    session is closed with its summary, the next start must not reopen it and
    overwrite that summary."""
    proj = server.workflow_project_create(title="stints", description="d")
    sid = uuid.uuid4().hex

    first = server.workflow_session_start(
        project_slug=proj["slug"], session_id=sid, phase="implementation"
    )
    server.workflow_session_end(first["session_slug"], summary="stint one")

    second = server.workflow_session_start(
        project_slug=proj["slug"], session_id=sid, phase="implementation"
    )
    assert second["existed"] is False
    assert second["session_slug"] != first["session_slug"]

    server.workflow_session_end(second["session_slug"], summary="stint two")
    done = server.workflow_project_complete(proj["slug"], outcome="two real stints")
    assert server.workflow_trace_get(done["trace_slug"])["session_count"] == 2


@requires_omnigraph
def test_duplicate_sessions_are_excluded_from_the_trace(server, tmp_state_dir):
    """A session marked superseded keeps its row but drops out of every
    aggregate, so a pre-fix duplicate can't inflate a trace."""
    from witan import server as srv

    proj = server.workflow_project_create(title="skewed", description="d")
    real = server.workflow_session_start(
        project_slug=proj["slug"], session_id=uuid.uuid4().hex, phase="spec"
    )
    server.workflow_session_end(real["session_slug"], summary="the actual work")
    dupe = server.workflow_session_start(
        project_slug=proj["slug"], session_id=uuid.uuid4().hex, phase="spec"
    )
    server.workflow_session_end(dupe["session_slug"], summary="artifact")

    srv.client.change(
        "mutations.gq",
        "update_workflow_session_superseded",
        {"slug": dupe["session_slug"], "superseded_by": real["session_slug"]},
    )

    assert (
        server.workflow_project_status(proj["slug"])["last_session"]["slug"]
        == (real["session_slug"])
    )
    done = server.workflow_project_complete(proj["slug"], outcome="one real session")
    assert server.workflow_trace_get(done["trace_slug"])["session_count"] == 1


# ── workflow_project_update ──────────────────────────────────────────────────


@requires_omnigraph
def test_project_update_edits_metadata(server):
    proj = server.workflow_project_create(
        title="old title", description="old", tags=["x"]
    )
    updated = server.workflow_project_update(
        proj["slug"],
        title="new title",
        description="new",
        tags=["y", "z"],
        github_issue="https://github.com/mitodl/agent-kit/issues/1",
    )
    assert updated["title"] == "new title"
    assert updated["description"] == "new"
    assert set(updated["tags"]) == {"y", "z"}
    assert updated["github_issue"].endswith("/issues/1")


@requires_omnigraph
def test_project_update_leaves_omitted_fields_alone(server):
    proj = server.workflow_project_create(
        title="keep me", description="keep this too", tags=["t"]
    )
    server.workflow_project_advance(proj["slug"], phase="spec")

    updated = server.workflow_project_update(proj["slug"], github_issue="i")
    assert updated["title"] == "keep me"
    assert updated["description"] == "keep this too"
    assert updated["tags"] == ["t"]
    assert updated["phase"] == "spec"  # untouched: advance() owns phase


@requires_omnigraph
def test_project_update_repos_replace_and_delta(server):
    a, b, c = (f"https://github.com/x/{n}" for n in ("a", "b", "c"))
    proj = server.workflow_project_create(title="repos", description="d", repos=[a])

    replaced = server.workflow_project_update(proj["slug"], repos=[b])
    assert replaced["repos"] == [b]

    added = server.workflow_project_update(proj["slug"], add_repos=[a, c])
    assert set(added["repos"]) == {a, b, c}

    # A repo can be removed — repos are a plain list field, not append-only edges.
    removed = server.workflow_project_update(proj["slug"], remove_repos=[b])
    assert set(removed["repos"]) == {a, c}
    assert proj["slug"] not in {p["slug"] for p in server.workflow_project_list(repo=b)}
    assert proj["slug"] in {p["slug"] for p in server.workflow_project_list(repo=c)}


@requires_omnigraph
def test_project_update_canonicalises_repo_case(server):
    canonical = "https://github.com/mitodl/agent-kit"
    proj = server.workflow_project_create(title="case", description="d")
    updated = server.workflow_project_update(
        proj["slug"], add_repos=["https://GitHub.com/MITodl/Agent-Kit"]
    )
    assert canonical in updated["repos"]

    # ...and a differently-cased removal still matches what's stored.
    cleared = server.workflow_project_update(
        proj["slug"], remove_repos=["https://github.com/MITODL/agent-kit"]
    )
    assert canonical not in (cleared["repos"] or [])


@requires_omnigraph
def test_project_update_can_abandon_and_revive(server):
    proj = server.workflow_project_create(title="abandon", description="d")
    assert (
        server.workflow_project_update(proj["slug"], status="abandoned")["status"]
        == "abandoned"
    )
    assert proj["slug"] not in {p["slug"] for p in server.workflow_project_list()}
    assert proj["slug"] in {
        p["slug"] for p in server.workflow_project_list(status="abandoned")
    }

    assert (
        server.workflow_project_update(proj["slug"], status="active")["status"]
        == "active"
    )


@requires_omnigraph
def test_project_update_missing_slug_returns_none(server):
    assert server.workflow_project_update("wp-nope") is None


# ── migrate dedupe-sessions ──────────────────────────────────────────────────
#
# Sessions minted before workflow_session_start became re-entrant. Sharing a
# session_id is not enough to call two sessions duplicates — one id spans
# several working stints — so the migration keys on overlapping in time.


def _plant_session(srv, project_slug, session_id, started_at, ended_at, summary):
    """Write a session row directly, the way the pre-upsert tool used to."""
    slug = f"ws-{project_slug}-{uuid.uuid4().hex[:6]}"
    srv.client.change(
        "mutations.gq",
        "insert_workflow_session",
        {
            "slug": slug,
            "project_slug": project_slug,
            "session_id": session_id,
            "repo": None,
            "phase": "implementation",
            "summary": "",
            "author": "test",
            "tags": None,
            "started_at": started_at,
        },
    )
    srv.client.change(
        "mutations.gq", "link_belongs_to", {"from": slug, "to": project_slug}
    )
    if ended_at:
        srv.client.change(
            "mutations.gq",
            "update_workflow_session_end",
            {
                "slug": slug,
                "summary": summary,
                "tools_used": None,
                "files_changed": None,
                "ended_at": ended_at,
            },
        )
    return slug


@requires_omnigraph
def test_dedupe_marks_overlapping_empty_sessions(server):
    from witan import server as srv

    proj = server.workflow_project_create(title="dedupe overlap", description="d")
    sid = uuid.uuid4().hex
    real = _plant_session(srv, proj["slug"], sid, "2026-01-01T10:00:00Z", None, "")
    dup1 = _plant_session(srv, proj["slug"], sid, "2026-01-01T10:05:00Z", None, "")
    dup2 = _plant_session(srv, proj["slug"], sid, "2026-01-01T10:09:00Z", None, "")

    dry = srv.migrate_dedupe_sessions()
    assert dry["applied"] is False
    assert dry["marked"] == {dup1: real, dup2: real}
    # Dry run wrote nothing.
    assert (
        len(
            srv.client.read(
                "read.gq", "list_sessions_by_project", {"project_slug": proj["slug"]}
            )
        )
        == 3
    )

    applied = srv.migrate_dedupe_sessions(apply=True)
    assert applied["marked"] == {dup1: real, dup2: real}
    assert len(srv._project_sessions(proj["slug"])) == 1

    # Idempotent: the marked rows are skipped on a second pass.
    assert srv.migrate_dedupe_sessions(apply=True)["marked"] == {}


@requires_omnigraph
def test_dedupe_leaves_sequential_stints_alone(server):
    """Eight sessions under one $CLAUDE_SESSION_ID, each closed before the next
    began and each with its own summary — the real shape in the corpus. None of
    them is a duplicate and none may be touched."""
    from witan import server as srv

    proj = server.workflow_project_create(title="dedupe stints", description="d")
    sid = uuid.uuid4().hex
    for hour in range(8):
        _plant_session(
            srv,
            proj["slug"],
            sid,
            f"2026-01-01T{hour + 1:02d}:00:00Z",
            f"2026-01-01T{hour + 1:02d}:30:00Z",
            f"stint {hour}",
        )

    assert srv.migrate_dedupe_sessions(apply=True)["marked"] == {}
    assert len(srv._project_sessions(proj["slug"])) == 8


@requires_omnigraph
def test_dedupe_keeps_the_fullest_summary_and_reports_ambiguity(server):
    from witan import server as srv

    proj = server.workflow_project_create(title="dedupe ambiguous", description="d")
    sid = uuid.uuid4().hex
    # An anchor that never ended, so everything after overlaps it.
    empty = _plant_session(srv, proj["slug"], sid, "2026-02-01T10:00:00Z", None, "")
    short = _plant_session(
        srv, proj["slug"], sid, "2026-02-01T10:01:00Z", "2026-02-01T10:20:00Z", "brief"
    )
    full = _plant_session(
        srv,
        proj["slug"],
        sid,
        "2026-02-01T10:02:00Z",
        "2026-02-01T10:40:00Z",
        "a much fuller account of what this session actually did",
    )

    result = srv.migrate_dedupe_sessions(apply=True)
    # Only the summary-less session is auto-marked; the two that wrote real
    # summaries are reported for a human instead of guessed at.
    assert result["marked"] == {empty: full}
    assert len(result["needs_review"]) == 1
    reviewed = {s["slug"] for s in result["needs_review"][0]["sessions"]}
    assert reviewed == {short, full}

    # ...and the human's judgement is applied with extra_marks.
    manual = srv.migrate_dedupe_sessions(apply=True, extra_marks={short: full})
    assert manual["marked"] == {short: full}
    assert [s["slug"] for s in srv._project_sessions(proj["slug"])] == [full]


@requires_omnigraph
def test_dedupe_rejects_bogus_extra_marks(server):
    from witan import server as srv

    proj = server.workflow_project_create(title="dedupe bogus", description="d")
    real = _plant_session(
        srv, proj["slug"], uuid.uuid4().hex, "2026-03-01T10:00:00Z", None, ""
    )
    with pytest.raises(RuntimeError, match="no such session"):
        srv.migrate_dedupe_sessions(apply=True, extra_marks={"ws-nope": real})
    with pytest.raises(RuntimeError, match="cannot supersede itself"):
        srv.migrate_dedupe_sessions(apply=True, extra_marks={real: real})


@requires_omnigraph
def test_dedupe_ignores_the_stop_hook_placeholder_summary(server):
    """An auto-closed session carries a placeholder saying only that it ended;
    that must not count as content worth keeping."""
    from witan import server as srv

    proj = server.workflow_project_create(title="dedupe autoclose", description="d")
    sid = uuid.uuid4().hex
    anchor = _plant_session(srv, proj["slug"], sid, "2026-04-01T10:00:00Z", None, "")
    auto = _plant_session(
        srv,
        proj["slug"],
        sid,
        "2026-04-01T10:01:00Z",
        "2026-04-01T10:02:00Z",
        "Session ended (auto-closed by Stop hook — call workflow_session_end "
        "explicitly for a better summary)",
    )
    result = srv.migrate_dedupe_sessions(apply=True)
    assert result["marked"] == {auto: anchor}
    assert result["needs_review"] == []


@requires_omnigraph
def test_dedupe_follows_transitive_overlap_chains(server):
    """Overlap is transitive. s1 has ended by the time s3 starts, but s2 — itself
    a retry — is still open, so the fixed workflow_session_start would have
    handed back s2's handle and s3 would never have existed. Comparing only
    against the run's first session would miss it."""
    from witan import server as srv

    proj = server.workflow_project_create(title="dedupe chain", description="d")
    sid = uuid.uuid4().hex
    s1 = _plant_session(
        srv, proj["slug"], sid, "2026-05-01T10:00:00Z", "2026-05-01T10:10:00Z", "real"
    )
    s2 = _plant_session(
        srv, proj["slug"], sid, "2026-05-01T10:05:00Z", "2026-05-01T10:20:00Z", ""
    )
    s3 = _plant_session(
        srv, proj["slug"], sid, "2026-05-01T10:12:00Z", "2026-05-01T10:15:00Z", ""
    )
    # s3 starts after s1 ended — only s2's still-open window links it to the run.
    result = srv.migrate_dedupe_sessions(apply=True)
    assert result["marked"] == {s2: s1, s3: s1}
    assert [s["slug"] for s in srv._project_sessions(proj["slug"])] == [s1]


@requires_omnigraph
def test_dedupe_run_ends_when_every_member_has_closed(server):
    """The mirror of the chain case: once the whole run has closed, the next
    session is a new stint, not a duplicate — however long the run ran."""
    from witan import server as srv

    proj = server.workflow_project_create(title="dedupe chain end", description="d")
    sid = uuid.uuid4().hex
    s1 = _plant_session(
        srv, proj["slug"], sid, "2026-06-01T10:00:00Z", "2026-06-01T10:10:00Z", "first"
    )
    dup = _plant_session(
        srv, proj["slug"], sid, "2026-06-01T10:05:00Z", "2026-06-01T10:20:00Z", ""
    )
    later = _plant_session(
        srv, proj["slug"], sid, "2026-06-01T10:25:00Z", "2026-06-01T10:40:00Z", "second"
    )
    result = srv.migrate_dedupe_sessions(apply=True)
    assert result["marked"] == {dup: s1}
    assert sorted(s["slug"] for s in srv._project_sessions(proj["slug"])) == sorted(
        [s1, later]
    )


@requires_omnigraph
def test_concurrent_session_starts_collapse_to_one_handle(
    server, tmp_state_dir, monkeypatch
):
    """The check-then-insert isn't atomic, so two simultaneous starts can both
    find no open session and both insert. Simulated by blinding the pre-insert
    check for the first call only — the second then runs for real, sees both
    rows, and both callers come back with the same handle."""
    from witan import server as srv

    proj = server.workflow_project_create(title="race", description="d")
    sid = uuid.uuid4().hex

    # Blind only the first two checks — a blanket monkeypatch.undo() would also
    # revert the conftest fixture's redirect of srv.client to the throwaway graph.
    real_check = srv._open_session_for_key
    calls = {"n": 0}

    def _blind_first_two(*args, **kwargs):
        calls["n"] += 1
        return None if calls["n"] <= 2 else real_check(*args, **kwargs)

    monkeypatch.setattr(srv, "_open_session_for_key", _blind_first_two)
    racer_a = server.workflow_session_start(
        project_slug=proj["slug"], session_id=sid, phase="implementation"
    )
    racer_b = server.workflow_session_start(
        project_slug=proj["slug"], session_id=sid, phase="implementation"
    )

    # Both raced past the check and inserted, but converge on one handle.
    assert racer_b["session_slug"] == racer_a["session_slug"]

    # Two rows exist; exactly one is live, and it's the earlier of the two.
    rows = srv.client.read(
        "read.gq", "sessions_for_key", {"project_slug": proj["slug"], "session_id": sid}
    )
    assert len(rows) == 2
    live = [r for r in rows if not r.get("superseded_by")]
    assert [r["slug"] for r in live] == [racer_a["session_slug"]]
    assert len(srv._project_sessions(proj["slug"])) == 1

    # A later, non-racing call still finds the surviving session.
    third = server.workflow_session_start(
        project_slug=proj["slug"], session_id=sid, phase="implementation"
    )
    assert third["existed"] is True
    assert third["session_slug"] == racer_a["session_slug"]

    # ...and the collapsed duplicate never reaches the trace.
    server.workflow_session_end(racer_a["session_slug"], summary="the real work")
    done = server.workflow_project_complete(
        proj["slug"], outcome="one session, not two"
    )
    assert server.workflow_trace_get(done["trace_slug"])["session_count"] == 1


# ── witan session sweep ──────────────────────────────────────────────────────


def _patch_cli_server(monkeypatch, srv):
    """Point the CLI's `_srv()` at the test server and capture console output.

    Dispatching through `_srv()` is the load-bearing part: a direct
    OmnigraphClient would only ever work locally, which is the bug that created
    the leaked-session backlog in the first place.
    """
    from witan.cli import _common

    monkeypatch.setattr(_common, "_server", srv)
    printed = []
    monkeypatch.setattr(
        _common.console, "print", lambda *a, **kw: printed.append(str(a[0]))
    )
    return printed


def test_parse_duration_accepts_units_and_bare_seconds():
    from witan.cli.session import _parse_duration

    assert _parse_duration("6h") == 21600
    assert _parse_duration("30m") == 1800
    assert _parse_duration("2d") == 172800
    assert _parse_duration("45s") == 45
    assert _parse_duration("90") == 90
    with pytest.raises(ValueError, match="could not parse duration"):
        _parse_duration("soon")
    # A negative age puts the cutoff in the FUTURE, which would make every open
    # session look stale — so `--older-than -1h --yes` would sweep the lot.
    for negative in ("-1h", "-30m", "-5"):
        with pytest.raises(ValueError, match="cannot be negative"):
            _parse_duration(negative)


@requires_omnigraph
def test_session_list_open_only_excludes_ended_and_superseded(server, tmp_state_dir):
    proj = server.workflow_project_create(title="sweepable", description="x")
    open_s = server.workflow_session_start(proj["slug"], "sid-open", "implementation")
    ended = server.workflow_session_start(proj["slug"], "sid-ended", "implementation")
    server.workflow_session_end(ended["session_slug"], summary="done")

    all_rows = server.workflow_session_list(project_slug=proj["slug"])
    assert {r["slug"] for r in all_rows} == {
        open_s["session_slug"],
        ended["session_slug"],
    }
    # The by-project query filters ON project_slug and so omits the column;
    # the tool puts it back so both code paths return one row shape.
    assert all(r["project_slug"] == proj["slug"] for r in all_rows)

    open_rows = server.workflow_session_list(project_slug=proj["slug"], open_only=True)
    assert [r["slug"] for r in open_rows] == [open_s["session_slug"]]


@requires_omnigraph
def test_session_sweep_is_dry_by_default(server, tmp_state_dir, monkeypatch):
    """The whole point of the default: it must not write."""
    from witan.cli import session as session_cli

    printed = _patch_cli_server(monkeypatch, server)
    proj = server.workflow_project_create(title="dry", description="x")
    server.workflow_session_start(proj["slug"], "sid-1", "implementation")

    session_cli.session_sweep(older_than="0s", project=proj["slug"])

    assert "dry run" in "\n".join(printed)
    assert server.workflow_session_list(project_slug=proj["slug"], open_only=True)


@requires_omnigraph
def test_session_sweep_closes_with_yes(server, tmp_state_dir, monkeypatch):
    from witan.cli import session as session_cli

    _patch_cli_server(monkeypatch, server)
    proj = server.workflow_project_create(title="wet", description="x")
    started = server.workflow_session_start(proj["slug"], "sid-1", "implementation")

    session_cli.session_sweep(older_than="0s", project=proj["slug"], yes=True)

    assert server.workflow_session_list(project_slug=proj["slug"], open_only=True) == []
    rows = server.workflow_session_list(project_slug=proj["slug"])
    swept = next(r for r in rows if r["slug"] == started["session_slug"])
    assert swept["ended_at"]
    # The summary must be honest that this was a sweep, not a real checkpoint.
    assert "sweep" in swept["summary"]


@requires_omnigraph
def test_session_sweep_leaves_sessions_younger_than_the_threshold(
    server, tmp_state_dir, monkeypatch
):
    """Guards the one legitimately-running session against a sweep."""
    from witan.cli import session as session_cli

    printed = _patch_cli_server(monkeypatch, server)
    proj = server.workflow_project_create(title="young", description="x")
    server.workflow_session_start(proj["slug"], "sid-live", "implementation")

    session_cli.session_sweep(older_than="6h", project=proj["slug"], yes=True)

    assert "No sessions" in "\n".join(printed)
    assert server.workflow_session_list(project_slug=proj["slug"], open_only=True)


@requires_omnigraph
def test_session_sweep_is_idempotent(server, tmp_state_dir, monkeypatch):
    """Re-closing an already-closed session just re-stamps ended_at."""
    from witan.cli import session as session_cli

    _patch_cli_server(monkeypatch, server)
    proj = server.workflow_project_create(title="twice", description="x")
    server.workflow_session_start(proj["slug"], "sid-1", "implementation")

    session_cli.session_sweep(older_than="0s", project=proj["slug"], yes=True)
    session_cli.session_sweep(older_than="0s", project=proj["slug"], yes=True)

    assert server.workflow_session_list(project_slug=proj["slug"], open_only=True) == []


@requires_omnigraph
def test_session_sweep_clears_the_local_handle(server, tmp_state_dir, monkeypatch):
    """Otherwise a later Stop hook tries to re-close a session we just swept."""
    from witan import session_state
    from witan.cli import session as session_cli

    _patch_cli_server(monkeypatch, server)
    proj = server.workflow_project_create(title="handles", description="x")
    started = server.workflow_session_start(proj["slug"], "sid-h", "implementation")
    session_state.write_handle("sid-h", dict(started))

    session_cli.session_sweep(older_than="0s", project=proj["slug"], yes=True)

    assert session_state.read_handle("sid-h") is None
