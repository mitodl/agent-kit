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


def test_lease_expired_tolerates_naive_timestamp():
    # A legacy/hand-edited store may have a claimed_at without a tz offset.
    # Subtracting it from tz-aware now must not raise TypeError; a naive value is
    # read as UTC. (Build the strings from UTC wall-clock, stripped of tzinfo, so
    # the assertion is independent of the test machine's local timezone.)
    utc_now = datetime.now(timezone.utc)
    naive_recent = utc_now.replace(tzinfo=None).isoformat()
    assert readiness.lease_expired(naive_recent) is False
    naive_old = (
        (utc_now - timedelta(seconds=readiness.CLAIM_LEASE_SECONDS + 60))
        .replace(tzinfo=None)
        .isoformat()
    )
    assert readiness.lease_expired(naive_old) is True


def test_in_progress_missing_claim_and_no_updated_at_is_pickable():
    # No claimed_at and no updated_at fallback either → nothing to judge a lease
    # against → treated as expired → reclaimable (legacy/hand-edited row).
    assert readiness.status_pickable({"status": "in_progress", "claimed_at": None})


def test_in_progress_unleased_but_recently_updated_is_not_pickable():
    # This is the regression this task fixes: task_update(status="in_progress")
    # never stamped claimed_at, so a task started seconds ago via that path used
    # to read as instantly and permanently free. It must fall back to updated_at
    # and be treated as held while recent.
    task = {
        "status": "in_progress",
        "claimed_at": None,
        "updated_at": _iso(0),
    }
    assert not readiness.status_pickable(task)


def test_in_progress_unleased_and_stale_updated_at_is_reclaimable():
    # The abandonment path must not regress: no claimed_at, but updated_at is
    # older than the lease window → the holder likely crashed → reclaimable.
    task = {
        "status": "in_progress",
        "claimed_at": None,
        "updated_at": _iso(-(readiness.CLAIM_LEASE_SECONDS + 60)),
    }
    assert readiness.status_pickable(task)


def test_claimed_at_takes_precedence_over_updated_at():
    # A stale updated_at must not override a fresh claimed_at (e.g. a renewed
    # claim on a task whose other fields haven't otherwise changed recently).
    task = {
        "status": "in_progress",
        "claimed_at": _iso(0),
        "updated_at": _iso(-(readiness.CLAIM_LEASE_SECONDS + 60)),
    }
    assert not readiness.status_pickable(task)


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
        {
            # task_update(status="in_progress") path: no claimed_at, but started
            # (updated_at) recently. Must read as held, not free.
            "slug": "tk-unleased-recent",
            "status": "in_progress",
            "priority": "p0",
            "claimed_at": None,
            "updated_at": _iso(0),
        },
    ]
    got = [t["slug"] for t in readiness.filter_ready(tasks)]
    # closed, freshly-held, and freshly-started-unleased excluded; ordered p0, p1, p3.
    assert got == ["tk-p0", "tk-stale", "tk-p3"]


def test_filter_ready_unknown_blocker_treated_closed():
    tasks = [{"slug": "tk-x", "status": "open", "blocked_by": ["tk-gone"]}]
    assert [t["slug"] for t in readiness.filter_ready(tasks)] == ["tk-x"]
