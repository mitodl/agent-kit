"""End-to-end tests for session/project provenance (spec §5)."""

from .conftest import requires_omnigraph


@requires_omnigraph
def test_session_produced_memory(server, tmp_state_dir, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-prov-1")

    proj = server.workflow_project_create(title="prov", description="provenance test")
    sess = server.workflow_session_start(
        project_slug=proj["slug"], session_id="sess-prov-1", phase="implementation"
    )
    mem = server.memory_store(
        kind="lesson", title="learned", content="a finding", severity="info"
    )

    # Default flat list (no per-session breakdown).
    prov = server.workflow_project_memories(proj["slug"])
    assert mem["slug"] in {m["slug"] for m in prov["memories"]}
    assert prov["by_session"] == {}

    # Opt-in grouping adds the per-session breakdown.
    grouped = server.workflow_project_memories(proj["slug"], group_by_session=True)
    assert sess["session_slug"] in grouped["by_session"]
    assert mem["slug"] in {
        m["slug"] for m in grouped["by_session"][sess["session_slug"]]
    }


@requires_omnigraph
def test_store_without_active_session_is_fine(server, monkeypatch):
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    mem = server.memory_store(kind="pattern", title="p", content="no session here")
    assert mem["slug"].startswith("pat-")
    # No session → no provenance, but the project walk still works and is empty.
    assert server.workflow_project_memories("wp-none")["memories"] == []


@requires_omnigraph
def test_informed_memory_in_project_walk(server):
    proj = server.workflow_project_create(title="inf", description="informed test")
    mem = server.memory_store(kind="project_fact", title="f", content="a fact")
    server.workflow_project_link_memory(proj["slug"], mem["slug"])
    prov = server.workflow_project_memories(proj["slug"])
    assert mem["slug"] in {m["slug"] for m in prov["memories"]}


@requires_omnigraph
def test_stale_session_state_does_not_block_store(server, tmp_state_dir, monkeypatch):
    # Handle points at a session that doesn't exist in the store. The engine
    # rejects the SessionProduced edge, but the memory write must still succeed.
    from witan import server as srv
    from witan import session_state

    session_state.write_handle("stale", {"session_slug": "ws-does-not-exist"})
    monkeypatch.setenv("CLAUDE_SESSION_ID", "stale")
    # Guard against the isolation silently going dead again: if the handle were
    # not being read, this would be None and the test below would pass vacuously.
    assert srv._active_session_slug() == "ws-does-not-exist"

    mem = server.memory_store(kind="lesson", title="x", content="y", severity="info")
    assert mem["slug"].startswith("les-")


@requires_omnigraph
def test_non_dict_state_file_is_ignored(server, tmp_state_dir, monkeypatch):
    from witan import server as srv
    from witan import session_state

    # Valid JSON, not an object — a truncated write can leave this behind.
    session_state.session_state_path("weird").write_text("[]")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "weird")

    # Must not raise AttributeError; just no active session.
    assert srv._active_session_slug() is None
    mem = server.memory_store(kind="pattern", title="p", content="z")
    assert mem["slug"].startswith("pat-")


@requires_omnigraph
def test_explicit_session_slug_carries_provenance_under_a_deployment(
    server, monkeypatch
):
    """A deployed replica can only learn the session from the tool argument.

    Simulated by forcing ``_active_session_slug`` to None — exactly what
    ``_is_local_stdio()`` produces once an OIDC issuer is configured.
    """
    from witan import server as srv

    monkeypatch.setattr(srv, "_active_session_slug", lambda: None)

    proj = server.workflow_project_create(title="depl", description="deployed store")
    sess = server.workflow_session_start(
        project_slug=proj["slug"], session_id="sess-depl-1", phase="implementation"
    )
    mem = server.memory_store(
        kind="lesson",
        title="threaded",
        content="handle came in as an argument",
        severity="info",
        session_slug=sess["session_slug"],
    )

    assert mem["session_linked"] is True
    assert "note" not in mem
    grouped = server.workflow_project_memories(proj["slug"], group_by_session=True)
    assert mem["slug"] in {
        m["slug"] for m in grouped["by_session"][sess["session_slug"]]
    }


@requires_omnigraph
def test_explicit_session_slug_wins_over_the_local_handle(server, monkeypatch):
    from witan import server as srv

    proj = server.workflow_project_create(title="pref", description="precedence")
    ambient = server.workflow_session_start(
        project_slug=proj["slug"], session_id="sess-ambient", phase="implementation"
    )
    explicit = server.workflow_session_start(
        project_slug=proj["slug"], session_id="sess-explicit", phase="implementation"
    )
    monkeypatch.setattr(srv, "_active_session_slug", lambda: ambient["session_slug"])

    mem = server.memory_store(
        kind="pattern",
        title="pref",
        content="argument beats ambient",
        session_slug=explicit["session_slug"],
    )

    grouped = server.workflow_project_memories(proj["slug"], group_by_session=True)
    by_session = {
        slug: {m["slug"] for m in mems} for slug, mems in grouped["by_session"].items()
    }
    assert mem["slug"] in by_session[explicit["session_slug"]]
    assert mem["slug"] not in by_session.get(ambient["session_slug"], set())


@requires_omnigraph
def test_trace_mine_threads_session_slug_to_mined_memories(server, monkeypatch):
    from witan import server as srv

    monkeypatch.setattr(srv, "_active_session_slug", lambda: None)

    proj = server.workflow_project_create(title="mine", description="mining test")
    sess = server.workflow_session_start(
        project_slug=proj["slug"], session_id="sess-mine-1", phase="delivery"
    )
    server.workflow_session_end(
        session_slug=sess["session_slug"], summary="did the thing"
    )
    trace = server.workflow_project_complete(proj["slug"], outcome="shipped")

    mined = server.workflow_trace_mine(
        trace_slug=trace["trace_slug"],
        lessons=[{"title": "mined lesson", "content": "worth remembering"}],
        session_slug=sess["session_slug"],
    )

    grouped = server.workflow_project_memories(proj["slug"], group_by_session=True)
    assert set(mined["created_lessons"]) <= {
        m["slug"] for m in grouped["by_session"][sess["session_slug"]]
    }


@requires_omnigraph
def test_malformed_session_id_is_rejected(server, monkeypatch):
    from witan import server as srv

    monkeypatch.setenv("CLAUDE_SESSION_ID", "../../etc/passwd")
    assert srv._active_session_slug() is None
