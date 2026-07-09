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
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time

from . import session_state
from ._detach import popen_detached

# Opportunistic optimize runs at most once per this window. Optimize takes the
# store's write lock and is ~tens of seconds on a bloated store, so daily is a
# safe default; override with WITAN_OPTIMIZE_INTERVAL (seconds; 0 disables).
_OPTIMIZE_INTERVAL = 24 * 3600.0

_REMOTE_PREFIXES = ("http://", "https://", "s3://")


def optimize_interval() -> float:
    """Throttle window in seconds; ``0`` (or negative) disables auto-optimize."""
    raw = os.environ.get("WITAN_OPTIMIZE_INTERVAL")
    if raw is None:
        return _OPTIMIZE_INTERVAL
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _OPTIMIZE_INTERVAL


def _stamp_file(graph_uri: str):
    digest = hashlib.sha1(graph_uri.encode()).hexdigest()[:16]
    return session_state.session_state_dir() / f"witan-optimize-{digest}.json"


def _last_run(graph_uri: str) -> float:
    try:
        return float(json.loads(_stamp_file(graph_uri).read_text()).get("stamp", 0.0))
    except Exception:  # noqa: BLE001 — missing/corrupt stamp → treat as never run
        return 0.0


def _mark_run(graph_uri: str, when: float) -> None:
    """Record the last-optimize time atomically.

    Concurrent Stop hooks (or an interrupted write) could otherwise leave a
    half-written stamp that ``_last_run`` reads as "never run", defeating the
    throttle and letting optimize spawn repeatedly. Write a process-unique temp
    file and ``os.replace`` it in, so a reader always sees a complete file.
    """
    path = _stamp_file(graph_uri)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps({"stamp": when}))
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def due(graph_uri: str, now: float | None = None) -> bool:
    """Whether an opportunistic optimize is due for ``graph_uri``.

    False when auto-optimize is disabled, the store is remote (maintained
    server-side, not by a client hook), or the throttle window hasn't elapsed.
    """
    interval = optimize_interval()
    if interval <= 0:
        return False
    if graph_uri.startswith(_REMOTE_PREFIXES):
        return False
    now = time.time() if now is None else now
    return now - _last_run(graph_uri) >= interval


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
