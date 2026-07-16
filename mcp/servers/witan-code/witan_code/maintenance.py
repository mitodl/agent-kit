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


def optimize_interval() -> float:
    """Throttle window in seconds; ``0`` (or negative) disables auto-optimize."""
    return _throttle.resolve_interval(
        "WITAN_CODE_OPTIMIZE_INTERVAL", _OPTIMIZE_INTERVAL
    )


def _stamp_file(store: str | Path) -> Path:
    digest = hashlib.sha256(str(store).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"witan-code-optimize-{digest}.json"


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
        popen_detached(
            [sys.executable, "-m", "witan_code", "optimize", "--store", str(store)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(os.environ),
        )
        return True
    except OSError:
        return False
