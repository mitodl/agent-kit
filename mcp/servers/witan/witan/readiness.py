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
    """True when an advisory claim's lease has elapsed (or there is no claim).

    Tolerates a naive ``claimed_at`` (a legacy/hand-edited store may lack a tz
    offset): a naive timestamp is read as UTC so the subtraction can't raise
    ``TypeError`` (offset-naive vs offset-aware) and crash ``task_ready``.
    """
    if not claimed_at:
        return True
    try:
        started = datetime.fromisoformat(claimed_at)
    except (ValueError, TypeError):
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - started).total_seconds() > CLAIM_LEASE_SECONDS


def status_pickable(task: dict, *, now: datetime | None = None) -> bool:
    """True when a task's status makes it claimable, ignoring blockers.

    ``open``/``blocked`` are always pickable; an ``in_progress`` task is only
    pickable once its lease has lapsed (the holder likely crashed). ``closed``
    is never pickable.

    ``claimed_at`` is only stamped by ``task_claim``; a task moved to
    ``in_progress`` through ``task_update`` (the skill/CLI "mark started" path)
    has no claim timestamp at all. Treating that as "no lease → free" would make
    it pickable from the instant it starts, forever — exactly the double-work
    bug this function exists to prevent. So when there is no ``claimed_at`` we
    fall back to ``updated_at`` (stamped on every write, including the status
    transition itself): recently started reads as held, and only a task that
    has sat untouched past the lease window reads as abandoned/reclaimable.
    """
    status = task.get("status")
    if status == "in_progress":
        lease_started_at = task.get("claimed_at") or task.get("updated_at")
        return lease_expired(lease_started_at, now=now)
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
