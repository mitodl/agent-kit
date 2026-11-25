"""The agent session id under Claude Code, Pi and Codex.

The parked workflow session handle, the Stop hook's auto-close and the
local-stdio provenance fallback resolve the session through
``session_state.current_session_id``: an explicit value, else
``$CLAUDE_SESSION_ID``, else ``$PI_SESSION_ID``, else ``$CODEX_SESSION_ID``,
else a runner's ``$WITAN_SESSION_ID``. A task claim's holder and the remote
proxy's injected ``session_id`` resolve it through
``session_state.claim_session_id``, which reads ``$WITAN_SESSION_ID`` first so
a runner's worker slots never share a claim.
Claude-keyed behaviour must be byte-identical to what it was before Pi was a
source; Pi and Codex must get the same auto-close Claude does. Codex's Stop hook
has no session variable, so ``witan session-checkpoint`` reads the id from the
hook's stdin there.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from witan import session_state

from .conftest import requires_omnigraph

PI_SID = "019a1b2c-3d4e-7f00-8a9b-0c1d2e3f4a5b"
CLAUDE_SID = "aaaaaaaa-1111-2222-3333-444444444444"
CODEX_SID = "019a2f3e-5c6d-7e8f-9a0b-1c2d3e4f5a6b"


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


def test_codex_id_is_the_last_fallback(monkeypatch):
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("PI_SESSION_ID", raising=False)
    monkeypatch.setenv("CODEX_SESSION_ID", CODEX_SID)
    assert session_state.current_session_id() == CODEX_SID

    monkeypatch.setenv("PI_SESSION_ID", PI_SID)
    assert session_state.current_session_id() == PI_SID


def test_the_two_resolution_orders():
    """A claim reads the runner's slot id first; the handle reads it last."""
    assert session_state.SESSION_ID_ENV_VARS == (
        "WITAN_SESSION_ID",
        "CLAUDE_SESSION_ID",
        "PI_SESSION_ID",
        "CODEX_SESSION_ID",
    )
    assert session_state.HANDLE_SESSION_ID_ENV_VARS == (
        "CLAUDE_SESSION_ID",
        "PI_SESSION_ID",
        "CODEX_SESSION_ID",
        "WITAN_SESSION_ID",
    )


def test_a_runners_id_decides_a_claim(monkeypatch):
    """A runner sets ``$WITAN_SESSION_ID`` per worker slot, and it has to decide
    the claim holder under every harness, including one that exports an id of
    its own."""
    monkeypatch.setenv("WITAN_SESSION_ID", "slot-1")
    monkeypatch.setenv("CLAUDE_SESSION_ID", CLAUDE_SID)
    monkeypatch.setenv("PI_SESSION_ID", PI_SID)
    monkeypatch.setenv("CODEX_SESSION_ID", CODEX_SID)
    assert session_state.claim_session_id() == "slot-1"
    assert session_state.claim_session_id("explicit-1") == "explicit-1"

    monkeypatch.setenv("WITAN_SESSION_ID", "")
    assert session_state.claim_session_id() == CLAUDE_SID


def test_a_runners_id_is_the_handles_last_fallback(monkeypatch):
    """The handle stays keyed by the harness id the agent passes to
    ``workflow_session_start`` and the checkpoints resolve; a harness that
    exports none (Copilot, OpenCode) falls back to the slot id."""
    monkeypatch.setenv("WITAN_SESSION_ID", "slot-1")
    monkeypatch.setenv("CLAUDE_SESSION_ID", CLAUDE_SID)
    monkeypatch.setenv("PI_SESSION_ID", PI_SID)
    assert session_state.current_session_id() == CLAUDE_SID

    monkeypatch.delenv("CLAUDE_SESSION_ID")
    assert session_state.current_session_id() == PI_SID

    monkeypatch.delenv("PI_SESSION_ID")
    monkeypatch.setenv("CODEX_SESSION_ID", CODEX_SID)
    assert session_state.current_session_id() == CODEX_SID

    monkeypatch.delenv("CODEX_SESSION_ID")
    assert session_state.current_session_id() == "slot-1"


def test_remote_proxy_sends_the_runners_id(monkeypatch):
    from witan.remote.proxy import RemoteServerProxy

    monkeypatch.setenv("WITAN_SESSION_ID", "slot-1")
    monkeypatch.setenv("CLAUDE_SESSION_ID", CLAUDE_SID)
    assert RemoteServerProxy._resolve_session_id(None) == "slot-1"


def test_remote_proxy_finds_the_handle_by_the_harness_id(tmp_state_dir, monkeypatch):
    """Under a runner the proxy's ``session_slug`` still comes from the handle
    parked under the harness id, not the slot id its claims use."""
    from witan.remote.proxy import RemoteServerProxy

    monkeypatch.setenv("WITAN_SESSION_ID", "slot-1")
    monkeypatch.setenv("CLAUDE_SESSION_ID", CLAUDE_SID)
    assert session_state.write_handle(CLAUDE_SID, {"session_slug": "ws-claude"})
    assert session_state.write_handle("slot-1", {"session_slug": "ws-slot"})
    assert RemoteServerProxy._resolve_session_slug(None) == "ws-claude"


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


@requires_omnigraph
def test_claude_checkpoint_under_a_runner_finds_the_claude_handle(server, monkeypatch):
    """The agent passes ``$CLAUDE_SESSION_ID`` to ``workflow_session_start``
    as the skills say; a runner's slot id in the Stop hook's env must not send
    the close looking under a different id."""
    from witan import server as srv
    from witan.cli import _common, hooks

    monkeypatch.setattr(_common, "_server", srv)
    proj = server.workflow_project_create(title="runner claude", description="d")
    handle = server.workflow_session_start(
        project_slug=proj["slug"], session_id=CLAUDE_SID, phase="spec"
    )
    monkeypatch.setenv("WITAN_SESSION_ID", "slot-1")
    monkeypatch.setenv("CLAUDE_SESSION_ID", CLAUDE_SID)

    hooks.session_checkpoint()

    assert _closed(srv, proj["slug"], handle["session_slug"])
    assert session_state.read_handle(CLAUDE_SID) is None


@requires_omnigraph
def test_cli_session_start_under_a_runner_is_closed_by_the_pi_checkpoint(
    server, monkeypatch
):
    """``witan session start`` run from Pi's bash tool under a runner sees
    both ids. It parks the handle under the Pi id, because Pi's checkpoint
    child carries only that one."""
    from witan import server as srv
    from witan.cli import _common, hooks
    from witan.cli import session as session_cli

    monkeypatch.setattr(_common, "_server", srv)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("WITAN_SESSION_ID", "slot-1")
    monkeypatch.setenv("PI_SESSION_ID", PI_SID)
    proj = server.workflow_project_create(title="runner pi", description="d")
    session_cli.session_start(proj["slug"], phase="spec")
    handle = session_state.read_handle(PI_SID)
    assert handle and handle["session_id"] == PI_SID

    # The extension's checkpoint child: the slot id dropped, the Pi id set.
    monkeypatch.delenv("WITAN_SESSION_ID")
    hooks.session_checkpoint()

    assert _closed(srv, proj["slug"], handle["session_slug"])


@requires_omnigraph
def test_cli_session_start_without_a_harness_id_parks_under_the_slot_id(
    server, monkeypatch
):
    """Copilot and OpenCode export no id, so the slot id keys the handle and
    the Stop hook in the same env finds it."""
    from witan import server as srv
    from witan.cli import _common, hooks
    from witan.cli import session as session_cli

    monkeypatch.setattr(_common, "_server", srv)
    for name in session_state.HARNESS_SESSION_ID_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WITAN_SESSION_ID", "slot-1")
    proj = server.workflow_project_create(title="runner bare", description="d")
    session_cli.session_start(proj["slug"], phase="spec")
    handle = session_state.read_handle("slot-1")
    assert handle and handle["session_id"] == "slot-1"

    hooks.session_checkpoint()

    assert _closed(srv, proj["slug"], handle["session_slug"])


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


# ── the Codex Stop hook ──────────────────────────────────────────────────────


def _codex_stop_payload(cwd) -> str:
    """A Codex ``Stop`` payload, field for field.

    ``StopCommandInput`` in codex-rs/hooks/src/schema.rs at rust-v0.162.1, as
    ``events/stop.rs`` fills it. ``session_id`` is ``Session::session_id``,
    the same id Codex exports to its shell tool as ``$CODEX_SESSION_ID``.
    ``turn_id`` is the Codex extension that tells this payload apart.
    """
    return json.dumps(
        {
            "session_id": CODEX_SID,
            "turn_id": "1",
            "transcript_path": None,
            "cwd": str(cwd),
            "hook_event_name": "Stop",
            "model": "gpt-5-codex",
            "permission_mode": "default",
            "stop_hook_active": False,
            "last_assistant_message": "Done.",
        }
    )


def _claude_stop_payload(cwd, session_id: str) -> str:
    return json.dumps(
        {
            "session_id": session_id,
            "transcript_path": str(Path(cwd) / "t.jsonl"),
            "cwd": str(cwd),
            "permission_mode": "default",
            "hook_event_name": "Stop",
            "stop_hook_active": False,
        }
    )


@requires_omnigraph
def test_codex_checkpoint_closes_the_session_named_on_stdin(
    server, monkeypatch, tmp_path, capsys
):
    """The Codex flow end to end: the agent passes ``$CODEX_SESSION_ID`` to
    ``workflow_session_start``, and the Stop hook, whose environment has no
    session variable, finds the handle by the payload's ``session_id``.

    A ``$CLAUDE_SESSION_ID`` inherited from a Claude session that launched
    Codex must not win: the payload names the session the hook fired for.
    Codex reads a Stop hook's non-JSON stdout as a failed hook, so the
    checkpoint must print nothing.
    """
    from witan import server as srv
    from witan.cli import _common, hooks

    monkeypatch.setattr(_common, "_server", srv)
    proj = server.workflow_project_create(title="codex stop", description="d")
    codex = server.workflow_session_start(
        project_slug=proj["slug"], session_id=CODEX_SID, phase="implementation"
    )
    claude = server.workflow_session_start(
        project_slug=proj["slug"], session_id=CLAUDE_SID, phase="implementation"
    )
    monkeypatch.setenv("CLAUDE_SESSION_ID", CLAUDE_SID)
    monkeypatch.setattr("sys.stdin", io.StringIO(_codex_stop_payload(tmp_path)))

    hooks.session_checkpoint()

    assert _closed(srv, proj["slug"], codex["session_slug"])
    assert session_state.read_handle(CODEX_SID) is None
    assert not _closed(srv, proj["slug"], claude["session_slug"])
    assert capsys.readouterr().out == ""


@requires_omnigraph
def test_claude_stop_payload_leaves_the_env_id_in_charge(server, monkeypatch, tmp_path):
    """Claude Code also writes a payload with a ``session_id``; it is not
    read, so the Claude path resolves ``$CLAUDE_SESSION_ID`` as before."""
    from witan import server as srv
    from witan.cli import _common, hooks

    monkeypatch.setattr(_common, "_server", srv)
    proj = server.workflow_project_create(title="claude stop", description="d")
    claude = server.workflow_session_start(
        project_slug=proj["slug"], session_id=CLAUDE_SID, phase="spec"
    )
    other = server.workflow_session_start(
        project_slug=proj["slug"], session_id=CODEX_SID, phase="spec"
    )
    monkeypatch.setenv("CLAUDE_SESSION_ID", CLAUDE_SID)
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(_claude_stop_payload(tmp_path, CODEX_SID))
    )

    hooks.session_checkpoint()

    assert _closed(srv, proj["slug"], claude["session_slug"])
    assert not _closed(srv, proj["slug"], other["session_slug"])


class _Tty(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize(
    "stdin",
    [
        _Tty('{"session_id": "x", "turn_id": "1"}'),
        io.StringIO(""),
        io.StringIO("not json"),
        io.StringIO('["session_id"]'),
    ],
    ids=["terminal", "empty", "not-json", "not-an-object"],
)
def test_hook_payload_is_empty_without_a_json_object_on_a_pipe(monkeypatch, stdin):
    from witan.cli import hooks

    monkeypatch.setattr("sys.stdin", stdin)
    assert hooks._hook_payload() == {}


def test_codex_checkpoint_keeps_a_config_error_off_stdout(
    tmp_state_dir, no_background_optimize, monkeypatch, tmp_path, capsys
):
    """A half-configured remote makes ``_srv()`` print its error, and Codex
    fails a Stop hook whose stdout isn't a JSON decision. The error belongs
    on stderr, and the handle stays for the next Stop to retry."""
    from witan.cli import _common, hooks

    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("")
    monkeypatch.setenv("WITAN_CONFIG", str(cfg_file))
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.setenv("WITAN_REMOTE_URL", "https://witan.invalid/mcp")
    monkeypatch.delenv("WITAN_OIDC_ISSUER", raising=False)
    monkeypatch.setattr(_common, "_server", None)
    assert session_state.write_handle(CODEX_SID, {"session_slug": "ws-codex"})
    monkeypatch.setattr("sys.stdin", io.StringIO(_codex_stop_payload(tmp_path)))

    hooks.session_checkpoint()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "OIDC" in captured.err
    assert session_state.read_handle(CODEX_SID) == {"session_slug": "ws-codex"}
