"""End-to-end tests for session/project provenance (spec §5)."""

from .conftest import requires_omnigraph


@requires_omnigraph
def test_session_produced_memory(server, tmp_path, monkeypatch):
    from witan import server as srv

    # Redirect the session-state file into the test's tmp dir for isolation.
    monkeypatch.setattr(
        srv, "_session_state_path", lambda sid: tmp_path / f"state-{sid}.json"
    )
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-prov-1")

    proj = server.workflow_project_create(title="prov", description="provenance test")
    sess = server.workflow_session_start(
        project_slug=proj["slug"], session_id="sess-prov-1", phase="implementation"
    )
    mem = server.memory_store(
        kind="lesson", title="learned", content="a finding", severity="info"
    )

    # Default flat list (no per-session breakdown).
    prov = server.project_memories(proj["slug"])
    assert mem["slug"] in {m["slug"] for m in prov["memories"]}
    assert prov["by_session"] == {}

    # Opt-in grouping adds the per-session breakdown.
    grouped = server.project_memories(proj["slug"], group_by_session=True)
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
    assert server.project_memories("wp-none")["memories"] == []


@requires_omnigraph
def test_informed_memory_in_project_walk(server):
    proj = server.workflow_project_create(title="inf", description="informed test")
    mem = server.memory_store(kind="project_fact", title="f", content="a fact")
    server.workflow_project_link_memory(proj["slug"], mem["slug"])
    prov = server.project_memories(proj["slug"])
    assert mem["slug"] in {m["slug"] for m in prov["memories"]}


@requires_omnigraph
def test_stale_session_state_does_not_block_store(server, tmp_path, monkeypatch):
    # State file points at a session that doesn't exist in the store. The engine
    # rejects the SessionProduced edge, but the memory write must still succeed.
    from witan import server as srv

    state = tmp_path / "state-stale.json"
    state.write_text('{"session_slug": "ws-does-not-exist"}')
    monkeypatch.setattr(srv, "_session_state_path", lambda sid: state)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "stale")

    mem = server.memory_store(kind="lesson", title="x", content="y", severity="info")
    assert mem["slug"].startswith("les-")


@requires_omnigraph
def test_non_dict_state_file_is_ignored(server, tmp_path, monkeypatch):
    from witan import server as srv

    state = tmp_path / "state-bad.json"
    state.write_text("[]")  # valid JSON, not an object
    monkeypatch.setattr(srv, "_session_state_path", lambda sid: state)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "weird")

    # Must not raise AttributeError; just no active session.
    assert srv._active_session_slug() is None
    mem = server.memory_store(kind="pattern", title="p", content="z")
    assert mem["slug"].startswith("pat-")


@requires_omnigraph
def test_malformed_session_id_is_rejected(server, monkeypatch):
    from witan import server as srv

    monkeypatch.setenv("CLAUDE_SESSION_ID", "../../etc/passwd")
    assert srv._active_session_slug() is None
