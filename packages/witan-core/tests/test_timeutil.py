from datetime import UTC, datetime, timedelta

import pytest

from witan_core import now_iso
from witan_core.timeutil import parse_timestamp


def test_now_iso_roundtrips_and_is_utc():
    value = now_iso()
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_now_iso_has_millisecond_precision():
    # omnigraph 0.12+ refuses a DateTime with non-zero digits past the third.
    fraction = now_iso().split(".")[1].removesuffix("+00:00")
    assert len(fraction) == 3


def test_parse_timestamp_reads_a_naive_value_as_utc():
    """The graph's naive value and now_iso()'s aware one compare as the same instant."""
    naive = parse_timestamp("2026-11-12T15:00:00.000")
    assert naive == datetime(2026, 11, 12, 15, tzinfo=UTC)
    assert naive == parse_timestamp("2026-11-12T15:00:00.000+00:00")
    assert naive == parse_timestamp("2026-11-12T15:00:00Z")


def test_parse_timestamp_keeps_an_offset():
    parsed = parse_timestamp("2026-11-12T10:00:00-05:00")
    assert parsed.utcoffset() == timedelta(hours=-5)
    assert parsed == datetime(2026, 11, 12, 15, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "error"), [("not a time", ValueError), (None, TypeError)]
)
def test_parse_timestamp_raises_for_the_caller_to_decide(value, error):
    with pytest.raises(error):
        parse_timestamp(value)
