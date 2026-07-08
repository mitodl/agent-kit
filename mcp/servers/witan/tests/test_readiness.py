"""Unit tests for the shared task-readiness predicate (no omnigraph needed)."""

from datetime import datetime, timedelta, timezone

from witan import readiness


def _iso(delta_seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat()


def test_open_and_blocked_are_pickable():
    assert readiness.status_pickable({"status": "open"})
    assert readiness.status_pickable({"status": "blocked"})


def test_closed_is_never_pickable():
    assert not readiness.status_pickable({"status": "closed"})


def test_in_progress_pickable_only_when_lease_expired():
    fresh = {"status": "in_progress", "claimed_at": _iso(0)}
    stale = {
        "status": "in_progress",
        "claimed_at": _iso(-(readiness.CLAIM_LEASE_SECONDS + 60)),
    }
    assert not readiness.status_pickable(fresh)
    assert readiness.status_pickable(stale)


def test_in_progress_missing_claim_is_pickable():
    # No claimed_at → lease treated as expired → reclaimable.
    assert readiness.status_pickable({"status": "in_progress", "claimed_at": None})


def test_is_ready_requires_all_blockers_closed():
    task = {"status": "open", "blocked_by": ["tk-a", "tk-b"]}
    status = {"tk-a": "closed", "tk-b": "open"}
    assert not readiness.is_ready(task, lambda s: status.get(s, "closed"))
    status["tk-b"] = "closed"
    assert readiness.is_ready(task, lambda s: status.get(s, "closed"))


def test_filter_ready_orders_by_priority_and_reclaims_expired():
    tasks = [
        {"slug": "tk-p3", "status": "open", "priority": "p3"},
        {"slug": "tk-p0", "status": "open", "priority": "p0"},
        {"slug": "tk-closed", "status": "closed", "priority": "p0"},
        {
            "slug": "tk-stale",
            "status": "in_progress",
            "priority": "p1",
            "claimed_at": _iso(-(readiness.CLAIM_LEASE_SECONDS + 60)),
        },
        {
            "slug": "tk-held",
            "status": "in_progress",
            "priority": "p0",
            "claimed_at": _iso(0),
        },
    ]
    got = [t["slug"] for t in readiness.filter_ready(tasks)]
    # closed and freshly-held excluded; ordered p0, p1, p3.
    assert got == ["tk-p0", "tk-stale", "tk-p3"]


def test_filter_ready_unknown_blocker_treated_closed():
    tasks = [{"slug": "tk-x", "status": "open", "blocked_by": ["tk-gone"]}]
    assert [t["slug"] for t in readiness.filter_ready(tasks)] == ["tk-x"]
