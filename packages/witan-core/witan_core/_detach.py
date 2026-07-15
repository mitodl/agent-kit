"""Cross-platform detached-subprocess spawning.

``subprocess.Popen``'s ``start_new_session=True`` (setsid) is POSIX-only — a
no-op on Windows, where a background hook child can otherwise get torn down
with its parent's process group/console. Used by every hook that must spawn
work and return immediately: the witan-code SessionStart indexer and both
servers' throttled optimize checkpoint (``maintenance.py``), spawned from the
``Stop`` hook.
"""

from __future__ import annotations

import subprocess
import sys


def popen_detached(args: list[str], **kwargs: object) -> subprocess.Popen:
    """Spawn ``args`` fully detached from the current process, cross-platform."""
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)  # noqa: S603 — caller controls argv
