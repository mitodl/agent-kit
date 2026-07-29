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
import re
import tempfile
from pathlib import Path

STATE_FILE_PREFIX = "workflow-session-"

# Session ids come from the environment ($CLAUDE_SESSION_ID) and are interpolated
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
    if is_safe_session_id(session_id):
        session_state_path(session_id).unlink(missing_ok=True)


def clear_handle_for_slug(session_slug: str) -> None:
    """Drop whichever handle file points at ``session_slug``.

    ``workflow_session_end`` is given a slug, not the session id that keyed the
    file, so the file is found by scanning. Best-effort.
    """
    for state_file in iter_session_state_files():
        try:
            data = json.loads(state_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("session_slug") == session_slug:
            state_file.unlink(missing_ok=True)
            return
