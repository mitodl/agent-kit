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


def parse_timestamp(value: str) -> datetime:
    """An ISO-8601 timestamp as an aware datetime, reading a naive one as UTC.

    The graph hands ``DateTime`` columns back without an offset (omnigraph
    drops it) while ``now_iso()`` writes ``+00:00``; both are UTC, and
    comparing the two shapes raises ``TypeError`` unless the naive one is
    given its zone. An offset that is present is kept, not converted.

    Raises ``ValueError`` for a string that won't parse and ``TypeError`` for
    a non-string, so each caller decides what an unreadable value means.
    """
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
