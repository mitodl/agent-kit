from datetime import datetime

from witan_core import now_iso


def test_now_iso_roundtrips_and_is_utc():
    value = now_iso()
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
