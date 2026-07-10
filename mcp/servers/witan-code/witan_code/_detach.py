"""Cross-platform detached-subprocess spawning.

``subprocess.Popen``'s ``start_new_session=True`` (setsid) is POSIX-only — a
no-op on Windows, where a background hook child can otherwise get torn down
with its parent's process group/console. Used by every hook that must spawn
work and return immediately: the SessionStart indexer and the throttled
optimize checkpoint (maintenance.py).

Deliberately duplicated in witan/witan/_detach.py (no cross-package import,
matching this package's existing convention — see graph.py's docstring).
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
