"""The agent session id under Claude Code and under Pi.

Everything that keys on "the current agent session" — the parked workflow
session handle, the Stop hook's auto-close, the local-stdio provenance and
task-claim fallbacks, the remote proxy's injected ``session_id`` — resolves it
through ``session_state.current_session_id``: an explicit value, else
``$CLAUDE_SESSION_ID``, else ``$PI_SESSION_ID``. Claude-keyed behaviour must be
byte-identical to what it was before Pi was a source; Pi must get the same
auto-close Claude does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from witan import session_state

from .conftest import requires_omnigraph

PI_SID = "019a1b2c-3d4e-7f00-8a9b-0c1d2e3f4a5b"
CLAUDE_SID = "aaaaaaaa-1111-2222-3333-444444444444"


# ── precedence ───────────────────────────────────────────────────────────────


def test_no_id_anywhere_is_empty(monkeypatch):
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("PI_SESSION_ID", raising=False)
    assert session_state.current_session_id() == ""
    assert session_state.current_session_id(None) == ""


def test_pi_id_is_used_when_claude_is_absent(monkeypatch):
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("PI_SESSION_ID", PI_SID)
    assert session_state.current_session_id() == PI_SID


def test_claude_id_beats_pi_id(monkeypatch):
    """Existing Claude-keyed handles and holder strings must not move."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", CLAUDE_SID)
    monkeypatch.setenv("PI_SESSION_ID", PI_SID)
    assert session_state.current_session_id() == CLAUDE_SID


def test_explicit_value_beats_both(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", CLAUDE_SID)
    monkeypatch.setenv("PI_SESSION_ID", PI_SID)
    assert session_state.current_session_id("explicit-1") == "explicit-1"


def test_empty_values_fall_through(monkeypatch):
    """An exported-but-empty variable must not mask the next source."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "")
    monkeypatch.setenv("PI_SESSION_ID", PI_SID)
    assert session_state.current_session_id("") == PI_SID


def test_remote_proxy_sends_the_pi_id(monkeypatch):
    """The CLI proxy's injected ``session_id`` (task-claim qualifier) and the
    parked-handle lookup behind its ``session_slug`` both follow the helper."""
    from witan.remote.proxy import RemoteServerProxy

    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("PI_SESSION_ID", PI_SID)
    assert RemoteServerProxy._resolve_session_id(None) == PI_SID

    monkeypatch.delenv("PI_SESSION_ID")
    assert RemoteServerProxy._resolve_session_id(None) is None


# ── handle round trip under Pi ───────────────────────────────────────────────


def test_handle_round_trips_under_the_pi_id(tmp_state_dir, monkeypatch):
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("PI_SESSION_ID", PI_SID)
    sid = session_state.current_session_id()
    assert session_state.write_handle(sid, {"session_slug": "ws-pi"})
    assert session_state.session_state_path(PI_SID).exists()
    assert session_state.read_handle(session_state.current_session_id()) == {
        "session_slug": "ws-pi"
    }


def test_missing_id_fails_soft(tmp_state_dir, monkeypatch):
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("PI_SESSION_ID", raising=False)
    assert session_state.read_handle(session_state.current_session_id()) is None
    assert session_state.write_handle(session_state.current_session_id(), {}) is False


@requires_omnigraph
def test_cli_session_start_parks_the_handle_under_the_pi_id(server, monkeypatch):
    from witan import server as srv
    from witan.cli import _common
    from witan.cli import session as session_cli

    monkeypatch.setattr(_common, "_server", srv)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("PI_SESSION_ID", PI_SID)

    proj = server.workflow_project_create(title="pi cli", description="d")
    session_cli.session_start(proj["slug"], phase="spec")

    handle = session_state.read_handle(PI_SID)
    assert handle and handle["session_id"] == PI_SID
    assert handle["session_slug"].startswith("ws-")


@requires_omnigraph
def test_provenance_fallback_reads_the_pi_handle(server, monkeypatch):
    """Local stdio: the server finds the active session by the Pi id too."""
    from witan import server as srv

    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    proj = server.workflow_project_create(title="pi prov", description="d")
    handle = server.workflow_session_start(
        project_slug=proj["slug"], session_id=PI_SID, phase="spec"
    )
    monkeypatch.setenv("PI_SESSION_ID", PI_SID)
    assert srv._active_session_slug() == handle["session_slug"]

    monkeypatch.delenv("PI_SESSION_ID")
    assert srv._active_session_slug() is None  # no id: fail soft, no link


# ── the Pi shutdown checkpoint ───────────────────────────────────────────────


def _closed(srv, project_slug: str, session_slug: str) -> bool:
    sessions = srv.client.read(
        "read.gq", "list_sessions_by_project", {"project_slug": project_slug}
    )
    return any(s["slug"] == session_slug and s["ended_at"] for s in sessions)


@requires_omnigraph
def test_pi_checkpoint_closes_the_session(server, monkeypatch):
    """The Pi flow end to end: the agent passes ``$PI_SESSION_ID`` as the
    ``session_id``, the local-stdio server parks the handle under it, and the
    extension's shutdown ``witan session-checkpoint`` — which sees only
    ``PI_SESSION_ID`` — finds it and closes the session."""
    from witan import server as srv
    from witan.cli import _common, hooks

    monkeypatch.setattr(_common, "_server", srv)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

    proj = server.workflow_project_create(title="pi shutdown", description="d")
    handle = server.workflow_session_start(
        project_slug=proj["slug"], session_id=PI_SID, phase="implementation"
    )
    monkeypatch.setenv("PI_SESSION_ID", PI_SID)

    hooks.session_checkpoint()

    assert _closed(srv, proj["slug"], handle["session_slug"])
    assert session_state.read_handle(PI_SID) is None


@requires_omnigraph
def test_claude_checkpoint_is_unchanged_when_both_ids_are_set(server, monkeypatch):
    """Claude wins: its handle is closed, a Pi-keyed one is left alone."""
    from witan import server as srv
    from witan.cli import _common, hooks

    monkeypatch.setattr(_common, "_server", srv)
    proj = server.workflow_project_create(title="both", description="d")
    claude = server.workflow_session_start(
        project_slug=proj["slug"], session_id=CLAUDE_SID, phase="spec"
    )
    pi = server.workflow_session_start(
        project_slug=proj["slug"], session_id=PI_SID, phase="spec"
    )
    monkeypatch.setenv("CLAUDE_SESSION_ID", CLAUDE_SID)
    monkeypatch.setenv("PI_SESSION_ID", PI_SID)

    hooks.session_checkpoint()

    assert _closed(srv, proj["slug"], claude["session_slug"])
    assert not _closed(srv, proj["slug"], pi["session_slug"])
    assert session_state.read_handle(PI_SID) is not None


def test_checkpoint_without_any_id_is_a_noop(
    tmp_state_dir, no_background_optimize, monkeypatch
):
    from witan.cli import _common, hooks

    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("PI_SESSION_ID", raising=False)

    class _MustNotBeCalled:
        def __getattr__(self, name):
            raise AssertionError(f"checkpoint reached the server ({name})")

    monkeypatch.setattr(_common, "_server", _MustNotBeCalled())
    hooks.session_checkpoint()  # must not raise, must not dispatch


# ── local task claims ────────────────────────────────────────────────────────


@requires_omnigraph
def test_local_claim_is_session_qualified_under_pi(server, monkeypatch):
    from witan import server as srv

    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("PI_SESSION_ID", PI_SID)
    t = server.task_create(title="pi claim", description="x")

    claimed = server.task_claim(t["slug"])

    assert claimed["claimed"] is True
    assert claimed["assignee"] == (
        f"{srv._current_author()}#{srv._session_suffix(PI_SID)}"
    )
    # An explicit argument still wins over the environment.
    t2 = server.task_create(title="pi claim explicit", description="x")
    explicit = server.task_claim(t2["slug"], session_id="explicit-sid")
    assert explicit["assignee"].endswith("#" + srv._session_suffix("explicit-sid"))


# ── the Pi extension forwards the id ─────────────────────────────────────────

_PACKAGE_EXT = (
    Path(__file__).resolve().parents[1]
    / "witan"
    / "extensions"
    / "pi"
    / "workflow-context.ts"
)
_MIRROR_EXT = (
    Path(__file__).resolve().parents[4]
    / "configs"
    / "pi"
    / "extensions"
    / "workflow-context.ts"
)


def test_pi_extension_forwards_the_session_id_to_the_checkpoint():
    """Pi puts ``PI_SESSION_ID`` only in its bash tool's command env, not in
    its own process env, so the shutdown handler must set it on the checkpoint
    child explicitly — and drop an inherited ``CLAUDE_SESSION_ID``, which the
    helper would otherwise prefer. Detached, so shutdown never waits on it.

    Both inherited ids are dropped even when Pi's own id is unavailable: an
    early return that let the child inherit the env unchanged would checkpoint
    the enclosing session's handle instead of no-oping."""
    src = _PACKAGE_EXT.read_text()
    assert "sessionManager?.getSessionId" in src
    assert "delete env.CLAUDE_SESSION_ID" in src
    assert "delete env.PI_SESSION_ID" in src
    assert "if (sessionId) env.PI_SESSION_ID = sessionId" in src
    assert "return undefined" not in src.split("function checkpointEnv", 1)[1]
    assert (
        'runInBackground(["session-checkpoint"], ctx?.cwd, checkpointEnv(ctx))' in src
    )
    assert "detached: true" in src
    # The id is set on the child's env copy only, never on Pi's own env.
    assert "process.env.PI_SESSION_ID =" not in src
    assert "process.env.CLAUDE_SESSION_ID" not in src


def test_pi_extension_mirror_is_byte_identical():
    if not _MIRROR_EXT.exists():
        pytest.skip("configs/pi mirror not present (installed package)")
    assert _MIRROR_EXT.read_bytes() == _PACKAGE_EXT.read_bytes()
