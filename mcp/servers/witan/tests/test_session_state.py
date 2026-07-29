"""Unit tests for the session-handle store (no omnigraph needed).

``workflow_session_start`` returns an explicit handle; this module parks it so
the Stop hook can pass it back to ``workflow_session_end``. Writer and reader are
separate processes, so path resolution and fail-soft reads are the whole
contract.
"""

import pytest

from witan import context, session_state


@pytest.fixture
def tmp_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    # tempfile caches gettempdir(); force it to re-read the env for this test.
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)
    return tmp_path


def test_path_uses_tempfile_and_prefix(tmp_state_dir):
    p = session_state.session_state_path("abc123")
    assert p.parent == tmp_state_dir
    assert p.name == "workflow-session-abc123.json"


def test_server_and_hook_agree_on_path(tmp_state_dir, monkeypatch):
    """The whole point of the module: writer (server) and reader (hook) resolve
    the same path even under a custom TMPDIR — the divergence bug (B6).

    Asserted behaviourally rather than by symbol identity: an ``is`` check on a
    re-exported alias keeps passing after the callers stop going through it,
    which is exactly how this assertion went dead once before.
    """
    from witan import server as srv

    monkeypatch.setenv("CLAUDE_SESSION_ID", "agree")
    # What the client (or a local-stdio server) writes...
    session_state.write_handle("agree", {"session_slug": "ws-agree"})
    # ...is what the server-side provenance reader finds.
    assert srv._active_session_slug() == "ws-agree"

    # And context resolves the same module (it shares the temp dir for caches).
    assert context.session_state is session_state


def test_iter_matches_written_files(tmp_state_dir):
    session_state.session_state_path("one").write_text("{}")
    session_state.session_state_path("two").write_text("{}")
    (tmp_state_dir / "unrelated.json").write_text("{}")

    names = {p.name for p in session_state.iter_session_state_files()}
    assert names == {"workflow-session-one.json", "workflow-session-two.json"}


def test_handle_round_trips(tmp_state_dir):
    handle = {"session_slug": "ws-x", "project_slug": "wp-y", "phase": "spec"}
    assert session_state.write_handle("sid-1", handle) is True
    assert session_state.read_handle("sid-1") == handle

    session_state.clear_handle("sid-1")
    assert session_state.read_handle("sid-1") is None


def test_read_handle_fails_soft(tmp_state_dir):
    """Nothing here may raise — the Stop hook must never block the agent."""
    assert session_state.read_handle("never-written") is None

    session_state.session_state_path("corrupt").write_text("{not json")
    assert session_state.read_handle("corrupt") is None

    # Valid JSON, wrong shape: a truncated write can leave `null` or `[]`.
    session_state.session_state_path("wrong-shape").write_text("[]")
    assert session_state.read_handle("wrong-shape") is None


@pytest.mark.parametrize("session_id", ["", "../escape", "a/b", "with space"])
def test_unsafe_session_ids_are_refused(session_id, tmp_state_dir):
    """The id comes from $CLAUDE_SESSION_ID and is interpolated into a filename,
    so a crafted value must not redirect the read or write out of the temp dir."""
    assert session_state.is_safe_session_id(session_id) is False
    assert session_state.write_handle(session_id, {"session_slug": "ws-x"}) is False
    assert session_state.read_handle(session_id) is None
    session_state.clear_handle(session_id)  # must not raise


def test_clear_handle_for_slug_finds_the_right_file(tmp_state_dir):
    """``workflow_session_end`` gets a slug, not the id that keyed the file."""
    session_state.write_handle("sid-a", {"session_slug": "ws-a"})
    session_state.write_handle("sid-b", {"session_slug": "ws-b"})
    session_state.session_state_path("sid-corrupt").write_text("{not json")

    session_state.clear_handle_for_slug("ws-b")

    assert session_state.read_handle("sid-a") == {"session_slug": "ws-a"}
    assert session_state.read_handle("sid-b") is None
    # An unparseable neighbour is skipped, not fatal.
    assert session_state.session_state_path("sid-corrupt").exists()
