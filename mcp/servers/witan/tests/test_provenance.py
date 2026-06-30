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

    prov = server.project_memories(proj["slug"])
    assert mem["slug"] in {m["slug"] for m in prov["memories"]}
    assert sess["session_slug"] in prov["by_session"]
    assert mem["slug"] in {m["slug"] for m in prov["by_session"][sess["session_slug"]]}


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
