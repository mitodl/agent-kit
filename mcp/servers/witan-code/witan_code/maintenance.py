"""Store compaction (omnigraph optimize/cleanup) with throttling.

Adapted for witan-code's per-repo, multi-store layout: there is no single
configured store to throttle. The current repo's per-repo store and the shared
cross-repo bridge store are each compacted and throttled independently, keyed by
the store's own path, so a bloated bridge store doesn't gate (or get gated by) a
repo store's compaction.

Every witan-code write — the ``PostToolUse`` single-file reindex, the
``SessionStart`` full index — appends a new tiny Lance fragment + manifest
version to a store, and left uncompacted it bloats until *opening* the store
dominates query latency, the same failure mode witan's own store hit (#98).
``omnigraph optimize`` collapses the fragments (non-destructive); ``cleanup``
GCs old versions to reclaim disk (destructive).

The ``Stop`` hook (``witan-code checkpoint``) calls
:func:`spawn_background_optimize` for whichever stores exist in the current
repo — at most once per interval each, detached so the hook returns
immediately. There is also a ``witan-code optimize`` / ``witan-code cleanup``
CLI for cron/systemd-timer driven maintenance.

From omnigraph 0.11 ``optimize`` no longer reclaims the storage of deleted
branches (``branches --prune``, ``reap-views --apply``); only ``cleanup`` does.
So on a slower cadence the same detached run also cleans up
(``--cleanup-older-than``), after optimize and in the same process so the two
never race. Retention matches the deployed weekly ``omnigraph-cleanup`` CronJob: age only,
never a version count. The window applies to a deleted branch too: on omnigraph
0.11.0 a freshly deleted fork survives ``cleanup --older-than 30d`` and is
reclaimed by ``--older-than 1s``, so its storage goes at the first cleanup
after its versions age past 30 days.

The throttle window, atomic last-run stamp, and due-check live in
``witan_core.maintenance``; this module supplies witan-code's own env var,
stamp-file location (a temp-dir digest), and detached command.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from witan_core import maintenance as _throttle
from witan_core import popen_detached

# Opportunistic optimize runs at most once per this window per store. Override
# with WITAN_CODE_OPTIMIZE_INTERVAL (seconds; 0 disables).
_OPTIMIZE_INTERVAL = _throttle.DEFAULT_OPTIMIZE_INTERVAL


# The cleanup piggybacks on an optimize run at most once per this window per
# store. Override with WITAN_CODE_CLEANUP_INTERVAL (seconds; 0 disables).
_CLEANUP_INTERVAL = 7 * 24 * 3600.0

# Versions younger than this survive a cleanup, so time-travel reads over the
# recent past keep working.
CLEANUP_OLDER_THAN = "30d"


def optimize_interval() -> float:
    """Throttle window in seconds; ``0`` (or negative) disables auto-optimize."""
    return _throttle.resolve_interval(
        "WITAN_CODE_OPTIMIZE_INTERVAL", _OPTIMIZE_INTERVAL
    )


def cleanup_interval() -> float:
    """Cleanup throttle window in seconds; ``0`` (or negative) disables it."""
    return _throttle.resolve_interval("WITAN_CODE_CLEANUP_INTERVAL", _CLEANUP_INTERVAL)


def _stamp_file(store: str | Path, op: str = "optimize") -> Path:
    digest = hashlib.sha256(str(store).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"witan-code-{op}-{digest}.json"


def _last_run(store: str | Path) -> float:
    return _throttle.last_run(_stamp_file(store))


def _mark_run(store: str | Path, when: float) -> None:
    _throttle.mark_run(_stamp_file(store), when)


def due(store: str | Path, now: float | None = None) -> bool:
    """Whether an opportunistic optimize is due for ``store``.

    False when auto-optimize is disabled, the store is remote (maintained
    server-side, not by a client hook), the store doesn't exist yet, or the
    throttle window hasn't elapsed.
    """
    return _throttle.is_due(
        store=store,
        stamp_file=_stamp_file(store),
        interval=optimize_interval(),
        now=time.time() if now is None else now,
        require_exists=True,
    )


def cleanup_due(store: str | Path, now: float | None = None) -> bool:
    """Whether the next opportunistic optimize of ``store`` should also clean up."""
    return _throttle.is_due(
        store=store,
        stamp_file=_stamp_file(store, "cleanup"),
        interval=cleanup_interval(),
        now=time.time() if now is None else now,
        require_exists=True,
    )


def spawn_background_optimize(store: str | Path, now: float | None = None) -> bool:
    """If an optimize is due for ``store``, detach one and return ``True``.

    The run also cleans up when :func:`cleanup_due`; a cleanup is never spawned
    on its own, so disabling optimize disables it too.

    Best-effort and non-blocking: the throttle stamps are written *before*
    spawning (so a failing run can't hot-loop every session), and the
    child is fully detached (its own session, no inherited stdio) so it
    outlives the Stop hook. Never raises — a maintenance failure must not fail
    the hook.
    """
    now = time.time() if now is None else now
    if not due(store, now):
        return False
    argv = [sys.executable, "-m", "witan_code", "optimize", "--store", str(store)]
    if cleanup_due(store, now):
        _throttle.mark_run(_stamp_file(store, "cleanup"), now)
        argv += ["--cleanup-older-than", CLEANUP_OLDER_THAN]
    _mark_run(store, now)
    try:
        popen_detached(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(os.environ),
        )
        return True
    except OSError:
        return False
