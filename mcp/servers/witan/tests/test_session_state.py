"""Unit tests for the shared session-state path helper (no omnigraph needed)."""

from witan import context, session_state


def test_path_uses_tempfile_and_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    # tempfile caches gettempdir(); force it to re-read the env for this test.
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)

    p = session_state.session_state_path("abc123")
    assert p.parent == tmp_path
    assert p.name == "workflow-session-abc123.json"


def test_server_and_hook_agree_on_path(tmp_path, monkeypatch):
    """The whole point of the module: writer (server) and reader (hook) resolve
    the same path even under a custom TMPDIR — the divergence bug (B6)."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)

    # The hook (context.session_checkpoint) builds the path via session_state;
    # the server helper is the same function. Assert they are literally shared.
    from witan import server

    assert server._session_state_path is session_state.session_state_path
    # And context uses it too (imported symbol identity).
    assert context.session_state is session_state


def test_iter_matches_written_files(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)

    session_state.session_state_path("one").write_text("{}")
    session_state.session_state_path("two").write_text("{}")
    (tmp_path / "unrelated.json").write_text("{}")

    names = {p.name for p in session_state.iter_session_state_files()}
    assert names == {"workflow-session-one.json", "workflow-session-two.json"}
