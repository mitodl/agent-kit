"""Small time helpers shared by both witan servers."""

from __future__ import annotations

from datetime import datetime, timezone


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string (the graph's timestamp format)."""
    return datetime.now(timezone.utc).isoformat()
