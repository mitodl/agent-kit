"""Small time helpers shared by both witan servers."""

from __future__ import annotations

from datetime import UTC, datetime


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string (the graph's timestamp format).

    Millisecond precision, because that is what an omnigraph ``DateTime``
    holds. 0.11 truncates anything finer on write; 0.12 and later refuse it
    ("fractional-second digits past the third must be zero").
    """
    return datetime.now(UTC).isoformat(timespec="milliseconds")
