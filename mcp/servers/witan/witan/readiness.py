"""Single source of truth for "is this task ready to work?".

``task_ready`` (server) and the context-injection hook both answer this, and
they used to answer it differently: the tool honored advisory-claim lease expiry
(an ``in_progress`` task whose holder crashed becomes reclaimable), the hook did
not. So the injected "Ready Tasks" list disagreed with ``task_ready()``. Both now
share ``status_pickable`` / ``is_ready`` / ``filter_ready`` from here.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

# Advisory-claim lease: a task left ``in_progress`` longer than this without being
# re-claimed is treated as abandoned (the holder likely crashed) and becomes
# reclaimable. Holders renew by calling ``task_claim`` again.
CLAIM_LEASE_SECONDS = 3600

_PRIORITY = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}


def lease_expired(claimed_at: str | None, *, now: datetime | None = None) -> bool:
    """True when an advisory claim's lease has elapsed (or there is no claim)."""
    if not claimed_at:
        return True
    try:
        started = datetime.fromisoformat(claimed_at)
    except (ValueError, TypeError):
        return True
    now = now or datetime.now(timezone.utc)
    return (now - started).total_seconds() > CLAIM_LEASE_SECONDS


def status_pickable(task: dict, *, now: datetime | None = None) -> bool:
    """True when a task's status makes it claimable, ignoring blockers.

    ``open``/``blocked`` are always pickable; an ``in_progress`` task is only
    pickable once its lease has lapsed (the holder likely crashed). ``closed``
    is never pickable.
    """
    status = task.get("status")
    if status == "in_progress":
        return lease_expired(task.get("claimed_at"), now=now)
    return status in ("open", "blocked")


def is_ready(
    task: dict,
    blocker_status: Callable[[str], str],
    *,
    now: datetime | None = None,
) -> bool:
    """Ready == status-pickable AND every blocker is closed.

    ``blocker_status`` resolves a blocker slug to its status; a missing blocker
    holds nothing back (callers return ``"closed"``).
    """
    if not status_pickable(task, now=now):
        return False
    return all(blocker_status(b) == "closed" for b in (task.get("blocked_by") or []))


def filter_ready(tasks: list[dict], *, now: datetime | None = None) -> list[dict]:
    """Ready tasks from a self-contained list, priority-ordered (p0 first).

    Blocker statuses are resolved within ``tasks`` — an unknown blocker is
    treated as closed. Use this when the full candidate set is already in hand
    (the context hook); ``task_ready`` supplies its own resolver that can fetch
    blockers outside the list.
    """
    status_by_slug = {t["slug"]: t.get("status") for t in tasks}

    def blocker_status(slug: str) -> str:
        return status_by_slug.get(slug) or "closed"

    ready = [t for t in tasks if is_ready(t, blocker_status, now=now)]
    ready.sort(key=lambda t: _PRIORITY.get(t.get("priority", "p3"), 9))
    return ready
