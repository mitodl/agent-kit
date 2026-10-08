from datetime import datetime

from witan_core import now_iso


def test_now_iso_roundtrips_and_is_utc():
    value = now_iso()
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_now_iso_has_millisecond_precision():
    # omnigraph 0.12+ refuses a DateTime with non-zero digits past the third.
    fraction = now_iso().split(".")[1].removesuffix("+00:00")
    assert len(fraction) == 3
