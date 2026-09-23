"""Local persistence for the session handle returned by ``workflow_session_start``.

``workflow_session_start`` returns an explicit handle — ``{"session_slug", …}``
— and the ``Stop`` hook (``witan session-checkpoint``) passes that handle back to
``workflow_session_end`` to auto-close the session. This module is where the
handle is parked in between, since the two run as separate processes.

The file is written by whichever process is **client-side**: the CLI after
``witan session start``, or the server itself only when it is the local stdio
server (same machine, same filesystem). A deployed server never writes it — with
several replicas behind a round-robin load balancer, the replica that served
``workflow_session_start`` shares nothing with the machine running the hook, so a
server-written file is at best useless and at worst a stale handle. This is the
state-management guidance in MCP 2026-07-28: applications needing state thread an
explicit tool-returned handle through the interaction rather than relying on
transport- or filesystem-level session state.

Everything here fails soft. A missing or unreadable handle means "no session to
close" — never an error, because the Stop hook must not block the agent.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

STATE_FILE_PREFIX = "workflow-session-"

# The agent-session id variables, in precedence order. Claude Code exports
# CLAUDE_SESSION_ID into every process it launches (hooks, the stdio MCP server).
# Pi exposes PI_SESSION_ID only to commands its `bash` tool runs, NOT to its own
# process env (Pi core `dist/core/tools/bash.js` `resolveSpawnContext` sets it
# per command from `ctx.sessionManager.getSessionId()`), so it reaches witan only
# when something forwards it: the agent running `echo $PI_SESSION_ID` and passing
# the value as an explicit argument, or the Pi workflow extension setting it on
# the `witan session-checkpoint` child it spawns at shutdown. Claude comes first
# so every existing Claude-keyed handle and holder string resolves exactly as
# before, even in a Claude session started from inside a Pi bash command.
SESSION_ID_ENV_VARS = ("CLAUDE_SESSION_ID", "PI_SESSION_ID")


def current_session_id(explicit: str | None = None) -> str:
    """The calling agent session's id, or ``""`` when none is known.

    Precedence: ``explicit`` (a caller-supplied value, e.g. an MCP tool's
    ``session_id`` argument) > ``$CLAUDE_SESSION_ID`` > ``$PI_SESSION_ID``.
    Empty strings are treated as unset at every level, so an exported-but-empty
    variable falls through to the next source rather than masking it.

    Returns ``""`` rather than None so callers can pass the result straight to
    :func:`read_handle` & co., which fail soft on an empty id. It is not
    validated here — the handle functions reject unsafe ids themselves, and the
    task-claim holder only digests the id.
    """
    if explicit:
        return explicit
    for name in SESSION_ID_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    return ""


# Session ids come from the environment (see SESSION_ID_ENV_VARS) and are interpolated
# into a filename, so anything that isn't a plain id is rejected rather than
# allowed to redirect a read or write out of the temp dir.
_SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9_.-]+")


def session_state_dir() -> Path:
    return Path(tempfile.gettempdir())


def is_safe_session_id(session_id: str) -> bool:
    return bool(session_id) and bool(_SAFE_SESSION_ID.fullmatch(session_id))


def session_state_path(session_id: str) -> Path:
    return session_state_dir() / f"{STATE_FILE_PREFIX}{session_id}.json"


def iter_session_state_files() -> list[Path]:
    return sorted(session_state_dir().glob(f"{STATE_FILE_PREFIX}*.json"))


def write_handle(session_id: str, handle: dict) -> bool:
    """Persist a session handle for the Stop hook. True if it landed."""
    if not is_safe_session_id(session_id):
        return False
    try:
        session_state_path(session_id).write_text(json.dumps(handle))
    except OSError:
        return False
    return True


def read_handle(session_id: str) -> dict | None:
    """The handle stored for ``session_id``, or None if absent/unusable."""
    if not is_safe_session_id(session_id):
        return None
    try:
        handle = json.loads(session_state_path(session_id).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    # A truncated write can be valid JSON but not an object (`null`, `[]`).
    return handle if isinstance(handle, dict) else None


def clear_handle(session_id: str) -> None:
    """Drop the handle for ``session_id``. Never raises.

    ``unlink`` itself can fail (read-only or sticky-bit temp dir, a shared
    ``/tmp`` where the file belongs to another user) — and this is called from
    the Stop hook, where an escaping OSError would break the "never blocks"
    contract.
    """
    if not is_safe_session_id(session_id):
        return
    try:
        session_state_path(session_id).unlink(missing_ok=True)
    except OSError:
        pass


def clear_handle_for_slug(session_slug: str) -> None:
    """Drop whichever handle file points at ``session_slug``.

    ``workflow_session_end`` is given a slug, not the session id that keyed the
    file, so the file is found by scanning. Best-effort: the unlink is inside the
    guard because this runs *after* the session-end mutation has committed, and
    an OSError escaping here would turn a successful close into a tool error.
    """
    for state_file in iter_session_state_files():
        try:
            data = json.loads(state_file.read_text())
            if isinstance(data, dict) and data.get("session_slug") == session_slug:
                state_file.unlink(missing_ok=True)
                return
        except (OSError, json.JSONDecodeError):
            continue
