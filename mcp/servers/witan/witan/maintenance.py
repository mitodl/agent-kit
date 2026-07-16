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


def optimize_interval() -> float:
    """Throttle window in seconds; ``0`` (or negative) disables auto-optimize."""
    return _throttle.resolve_interval("WITAN_OPTIMIZE_INTERVAL", _OPTIMIZE_INTERVAL)


def _stamp_file(graph_uri: str) -> Path:
    digest = hashlib.sha1(graph_uri.encode()).hexdigest()[:16]
    return session_state.session_state_dir() / f"witan-optimize-{digest}.json"


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


def spawn_background_optimize(graph_uri: str, now: float | None = None) -> bool:
    """If an optimize is due, detach one and return ``True``; else ``False``.

    Best-effort and non-blocking: the throttle stamp is written *before*
    spawning (so a failing optimize can't hot-loop every session), and the
    child is fully detached (its own session, no inherited stdio) so it outlives
    the Stop hook. Never raises — a maintenance failure must not fail the hook.
    """
    if not due(graph_uri, now):
        return False
    _mark_run(graph_uri, time.time() if now is None else now)
    try:
        popen_detached(
            [sys.executable, "-m", "witan", "optimize", "--store", graph_uri],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(os.environ),
        )
        return True
    except OSError:
        return False
