"""Throttle + atomic-stamp mechanics for opportunistic store compaction.

Both servers run ``omnigraph optimize`` opportunistically from their ``Stop``
hook, at most once per interval per store, never blocking the agent. The tricky,
error-prone parts are shared here; each server wraps them with its own store-key
type, env var, stamp-file location, and detached command (see
``witan.maintenance`` / ``witan_code.maintenance``):

- ``resolve_interval`` — parse the throttle window from an env var.
- ``mark_run`` — write the last-run stamp *atomically* (temp file + ``os.replace``)
  so a concurrent/interrupted write can't leave a half-written stamp that reads
  as "never run" and defeats the throttle.
- ``last_run`` — read it back, treating missing/corrupt as "never run".
- ``is_due`` — the disabled / remote-store / missing-store / window-elapsed logic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Remote stores (omnigraph-server) are compacted server-side, not by a client
# hook, so opportunistic optimize never fires for them.
REMOTE_PREFIXES = ("http://", "https://", "s3://")

# Optimize takes the store's write lock and is ~tens of seconds on a bloated
# store, so daily is a safe default throttle window.
DEFAULT_OPTIMIZE_INTERVAL = 24 * 3600.0


def resolve_interval(env_var: str, default: float = DEFAULT_OPTIMIZE_INTERVAL) -> float:
    """Throttle window in seconds from ``env_var``; ``0``/negative disables."""
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def last_run(stamp_file: Path) -> float:
    """Last-optimize timestamp from ``stamp_file``; ``0.0`` if missing/corrupt."""
    try:
        return float(json.loads(stamp_file.read_text()).get("stamp", 0.0))
    except Exception:  # noqa: BLE001 — missing/corrupt stamp → treat as never run
        return 0.0


def mark_run(stamp_file: Path, when: float) -> None:
    """Record the last-optimize time atomically.

    Concurrent Stop hooks (or an interrupted write) could otherwise leave a
    half-written stamp that ``last_run`` reads as "never run", defeating the
    throttle and letting optimize spawn repeatedly. Write a process-unique temp
    file and ``os.replace`` it in, so a reader always sees a complete file.
    """
    tmp = stamp_file.with_name(f"{stamp_file.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps({"stamp": when}))
        os.replace(tmp, stamp_file)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def is_due(
    *,
    store: str | Path,
    stamp_file: Path,
    interval: float,
    now: float,
    require_exists: bool,
) -> bool:
    """Whether an opportunistic optimize is due for ``store``.

    False when auto-optimize is disabled (``interval<=0``), the store is remote
    (maintained server-side), ``require_exists`` and the store doesn't exist
    yet, or the throttle window hasn't elapsed since ``last_run``.
    """
    if interval <= 0:
        return False
    if str(store).startswith(REMOTE_PREFIXES):
        return False
    if require_exists and not Path(store).exists():
        return False
    return now - last_run(stamp_file) >= interval
