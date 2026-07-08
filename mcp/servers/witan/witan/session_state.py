"""Single source of truth for the session-state temp-file path.

``workflow_session_start`` writes a small JSON state file so the ``Stop`` hook
(``session_checkpoint``) can auto-close the session. The writer (server) and the
reader (hook) MUST agree on the path, or the auto-close silently no-ops and the
session leaks open forever. Both now go through here — always ``tempfile``, which
honors ``TMPDIR``/``TEMP``/``TMP`` with the stdlib fallback+writability chain,
rather than an ad-hoc ``os.environ.get("TMPDIR", "/tmp")`` that diverges from it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

STATE_FILE_PREFIX = "workflow-session-"


def session_state_dir() -> Path:
    return Path(tempfile.gettempdir())


def session_state_path(session_id: str) -> Path:
    return session_state_dir() / f"{STATE_FILE_PREFIX}{session_id}.json"


def iter_session_state_files() -> list[Path]:
    return sorted(session_state_dir().glob(f"{STATE_FILE_PREFIX}*.json"))
