"""Store compaction (omnigraph optimize/cleanup) with throttling.

Every witan write appends a new tiny Lance fragment + manifest version, and the
store is never compacted on its own. Left alone it bloats until *opening* the
store dominates query latency — a fixed per-query cost independent of rows
returned — which is the root cause the inject-context output cache (#89) and
read-reduction (#91) only mitigated. ``omnigraph optimize`` collapses the
fragments (non-destructive); ``cleanup`` GCs old versions to reclaim disk
(destructive).

This module keeps the store compacted opportunistically without ever blocking
the agent: the ``Stop`` hook calls :func:`spawn_background_optimize`, which — at
most once per interval — detaches a ``witan optimize`` process and returns
immediately. There is also a ``witan optimize`` / ``witan cleanup`` CLI for
cron / systemd-timer driven maintenance.

From omnigraph 0.11 ``optimize`` no longer reclaims the storage of deleted
branches; only ``cleanup`` does. So on a slower cadence the same detached run
also cleans up (``--cleanup-older-than``), after optimize and in the same
process so the two never race. Retention matches the deployed weekly
``omnigraph-cleanup`` CronJob: age only, never a version count. The window
applies to a deleted branch too: on omnigraph 0.11.0 a freshly deleted fork
survives ``cleanup --older-than 30d`` and is reclaimed by ``--older-than 1s``,
so its storage goes at the first cleanup after its versions age past 30 days.

The throttle window, atomic last-run stamp, and due-check live in
``witan_core.maintenance``; this module supplies witan's own env var, stamp-file
location (keyed off ``session_state``), and detached command.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

from witan_core import maintenance as _throttle
from witan_core import popen_detached

from . import session_state

# Opportunistic optimize runs at most once per this window. Override with
# WITAN_OPTIMIZE_INTERVAL (seconds; 0 disables).
_OPTIMIZE_INTERVAL = _throttle.DEFAULT_OPTIMIZE_INTERVAL


# The cleanup piggybacks on an optimize run at most once per this window.
# Override with WITAN_CLEANUP_INTERVAL (seconds; 0 disables).
_CLEANUP_INTERVAL = 7 * 24 * 3600.0

# Versions younger than this survive a cleanup, so time-travel reads over the
# recent past keep working.
CLEANUP_OLDER_THAN = "30d"


def optimize_interval() -> float:
    """Throttle window in seconds; ``0`` (or negative) disables auto-optimize."""
    return _throttle.resolve_interval("WITAN_OPTIMIZE_INTERVAL", _OPTIMIZE_INTERVAL)


def cleanup_interval() -> float:
    """Cleanup throttle window in seconds; ``0`` (or negative) disables it."""
    return _throttle.resolve_interval("WITAN_CLEANUP_INTERVAL", _CLEANUP_INTERVAL)


def _stamp_file(graph_uri: str, op: str = "optimize") -> Path:
    digest = hashlib.sha1(graph_uri.encode()).hexdigest()[:16]
    return session_state.session_state_dir() / f"witan-{op}-{digest}.json"


def _last_run(graph_uri: str) -> float:
    return _throttle.last_run(_stamp_file(graph_uri))


def _mark_run(graph_uri: str, when: float) -> None:
    _throttle.mark_run(_stamp_file(graph_uri), when)


def due(graph_uri: str, now: float | None = None) -> bool:
    """Whether an opportunistic optimize is due for ``graph_uri``.

    False when auto-optimize is disabled, the store is remote (maintained
    server-side, not by a client hook), or the throttle window hasn't elapsed.
    """
    return _throttle.is_due(
        store=graph_uri,
        stamp_file=_stamp_file(graph_uri),
        interval=optimize_interval(),
        now=time.time() if now is None else now,
        require_exists=False,
    )


def cleanup_due(graph_uri: str, now: float | None = None) -> bool:
    """Whether the next opportunistic optimize should also clean up."""
    return _throttle.is_due(
        store=graph_uri,
        stamp_file=_stamp_file(graph_uri, "cleanup"),
        interval=cleanup_interval(),
        now=time.time() if now is None else now,
        require_exists=False,
    )


def spawn_background_optimize(graph_uri: str, now: float | None = None) -> bool:
    """If an optimize is due, detach one and return ``True``; else ``False``.

    The run also cleans up when :func:`cleanup_due`; a cleanup is never spawned
    on its own, so disabling optimize disables it too.

    Best-effort and non-blocking: the throttle stamps are written *before*
    spawning (so a failing run can't hot-loop every session), and the
    child is fully detached (its own session, no inherited stdio) so it outlives
    the Stop hook. Never raises — a maintenance failure must not fail the hook.
    """
    now = time.time() if now is None else now
    if not due(graph_uri, now):
        return False
    argv = [sys.executable, "-m", "witan", "optimize", "--store", graph_uri]
    if cleanup_due(graph_uri, now):
        _throttle.mark_run(_stamp_file(graph_uri, "cleanup"), now)
        argv += ["--cleanup-older-than", CLEANUP_OLDER_THAN]
    _mark_run(graph_uri, now)
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
