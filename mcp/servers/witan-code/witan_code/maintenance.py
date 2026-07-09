"""Store compaction (omnigraph optimize/cleanup) with throttling.

Mirrors witan's own ``witan.maintenance`` (deliberately duplicated — no
cross-package import, see ``graph.py``'s docstring), adapted for witan-code's
per-repo, multi-store layout: there is no single configured store to
throttle. The current repo's per-repo store and the shared cross-repo bridge
store are each compacted and throttled independently, keyed by the store's
own path, so a bloated bridge store doesn't gate (or get gated by) a repo
store's compaction.

Every witan-code write — the ``PostToolUse`` single-file reindex, the
``SessionStart`` full index — appends a new tiny Lance fragment + manifest
version to a store, and left uncompacted it bloats until *opening* the store
dominates query latency, the same failure mode witan's own store hit (#98).
``omnigraph optimize`` collapses the fragments (non-destructive); ``cleanup``
GCs old versions to reclaim disk (destructive).

The Stop hook (``codegraph-checkpoint.sh``) calls
:func:`spawn_background_optimize` for whichever stores exist in the current
repo — at most once per interval each, detached so the hook returns
immediately. There is also a ``witan-code optimize`` / ``witan-code cleanup``
CLI for cron/systemd-timer driven maintenance.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Opportunistic optimize runs at most once per this window per store. Optimize
# takes the store's write lock and is ~tens of seconds on a bloated store, so
# daily is a safe default; override with WITAN_CODE_OPTIMIZE_INTERVAL (seconds;
# 0 disables).
_OPTIMIZE_INTERVAL = 24 * 3600.0

_REMOTE_PREFIXES = ("http://", "https://", "s3://")


def optimize_interval() -> float:
    """Throttle window in seconds; ``0`` (or negative) disables auto-optimize."""
    raw = os.environ.get("WITAN_CODE_OPTIMIZE_INTERVAL")
    if raw is None:
        return _OPTIMIZE_INTERVAL
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _OPTIMIZE_INTERVAL


def _stamp_file(store: str | Path) -> Path:
    digest = hashlib.sha256(str(store).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"witan-code-optimize-{digest}.json"


def _last_run(store: str | Path) -> float:
    try:
        return float(json.loads(_stamp_file(store).read_text()).get("stamp", 0.0))
    except Exception:  # noqa: BLE001 — missing/corrupt stamp → treat as never run
        return 0.0


def _mark_run(store: str | Path, when: float) -> None:
    """Record the last-optimize time atomically.

    Concurrent Stop hooks (or an interrupted write) could otherwise leave a
    half-written stamp that ``_last_run`` reads as "never run", defeating the
    throttle and letting optimize spawn repeatedly. Write a process-unique temp
    file and ``os.replace`` it in, so a reader always sees a complete file.
    """
    path = _stamp_file(store)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps({"stamp": when}))
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def due(store: str | Path, now: float | None = None) -> bool:
    """Whether an opportunistic optimize is due for ``store``.

    False when auto-optimize is disabled, the store is remote (maintained
    server-side, not by a client hook), the store doesn't exist yet, or the
    throttle window hasn't elapsed.
    """
    interval = optimize_interval()
    if interval <= 0:
        return False
    if str(store).startswith(_REMOTE_PREFIXES):
        return False
    if not Path(store).exists():
        return False
    now = time.time() if now is None else now
    return now - _last_run(store) >= interval


def spawn_background_optimize(store: str | Path, now: float | None = None) -> bool:
    """If an optimize is due for ``store``, detach one and return ``True``.

    Best-effort and non-blocking: the throttle stamp is written *before*
    spawning (so a failing optimize can't hot-loop every session), and the
    child is fully detached (its own session, no inherited stdio) so it
    outlives the Stop hook. Never raises — a maintenance failure must not fail
    the hook.
    """
    if not due(store, now):
        return False
    _mark_run(store, time.time() if now is None else now)
    try:
        subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            [sys.executable, "-m", "witan_code", "optimize", "--store", str(store)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=dict(os.environ),
        )
        return True
    except OSError:
        return False
