"""A redaction must reach the caller (tk-write-path-redaction-silently-rewrites-content-a-aec2b6).

The defect these pin: the guard rewrote a field, returned success, and said
nothing — so an unrecoverable edit to the caller's data looked like a clean
write. Everything here is about the report, not the detection.
"""

import pytest

from witan.config import ScanConfig
from witan.scan import annotate, no_redactions, take_redactions, write_guard_from_config
from witan.scan.notice import describe

from .conftest import requires_omnigraph


@pytest.fixture(autouse=True)
def _isolated():
    """No notice may cross a test boundary — that is the bug being prevented."""
    with no_redactions():
        yield


def _guard(**kwargs):
    return write_guard_from_config(ScanConfig(enabled=True, **kwargs))


# A synthetic PAN: Visa's test number, Luhn-valid and not issued to anyone.
CARD = "4111 1111 1111 1111"


def test_redaction_is_recorded_with_the_offsets_of_the_original_value():
    guard = _guard(pii_action="redact")
    params = guard("insert_memory", {"title": "t", "content": f"pay {CARD} ok"})

    assert params["content"] == "pay «redacted:credit_card» ok"
    (notice,) = take_redactions()
    assert notice.query_name == "insert_memory"
    assert notice.field == "content"
    assert notice.detector == "credit_card"
    # The offsets index the value AS SENT, so the caller can find the span in
    # their own input — this is what stands in for quoting the match.
    assert f"pay {CARD} ok"[notice.start : notice.end] == CARD


def test_notice_never_carries_the_matched_text():
    """ADR 0001 §D3: a matched value must not travel in a report. The response
    is a worse place than a log for a `secret`-category match — it goes into the
    caller's transcript."""
    guard = _guard(secret_action="redact")
    secret = "AKIAIOSFODNN7EXAMPLE"  # pragma: allowlist secret gitleaks:allow
    guard("insert_memory", {"title": "t", "content": f"key {secret}"})

    notices = take_redactions()
    dumped = str([n.model_dump() for n in notices]) + describe(notices)
    assert secret not in dumped
    assert "credit" not in dumped  # sanity: it is the aws rule that fired


def test_a_clean_write_records_nothing():
    _guard()("insert_memory", {"title": "t", "content": "nothing sensitive here"})
    assert take_redactions() == ()


def test_a_blocked_write_records_nothing():
    """Nothing was persisted and nothing was rewritten, so there is no
    alteration to report. Reporting one would describe an edit that never
    happened."""
    from witan.scan import WriteBlocked

    guard = _guard(secret_action="block")
    with pytest.raises(WriteBlocked):
        guard(
            "insert_memory",
            {"title": "t", "content": "key AKIAIOSFODNN7EXAMPLE"},  # gitleaks:allow
        )
    assert take_redactions() == ()


def test_suppressed_findings_record_nothing():
    """An allowlisted match is downgraded to `warn` and never rewritten, so it
    is not an alteration either."""
    guard = _guard(pii_action="redact", allowlist=[CARD])
    params = guard("insert_memory", {"title": "t", "content": f"pay {CARD} ok"})
    assert params["content"] == f"pay {CARD} ok"
    assert take_redactions() == ()


def test_the_same_write_issued_twice_reports_one_redaction():
    """`_store_memory` retries its batch without the provenance edge when the
    session handle is stale. One rewrite must not be reported as two."""
    guard = _guard(pii_action="redact")
    params = {"title": "t", "content": f"pay {CARD} ok"}
    guard("insert_memory", params)
    guard("insert_memory", params)
    assert len(take_redactions()) == 1


def test_two_fields_are_reported_separately():
    guard = _guard(pii_action="redact")
    guard("insert_memory", {"title": f"card {CARD}", "content": f"pay {CARD} ok"})
    assert {n.field for n in take_redactions()} == {"title", "content"}


def test_taking_clears_so_a_notice_cannot_attach_to_the_next_write():
    guard = _guard(pii_action="redact")
    guard("insert_memory", {"title": "t", "content": f"pay {CARD} ok"})
    assert len(take_redactions()) == 1
    assert take_redactions() == ()


# ── annotate ─────────────────────────────────────────────────────────────────


def test_annotate_attaches_the_notice_and_a_readable_sentence():
    guard = _guard(pii_action="redact")
    guard("update_task", {"slug": "tk-x", "description": f"pay {CARD} ok"})

    result = annotate({"slug": "tk-x"})
    assert result["slug"] == "tk-x"
    assert result["redactions"][0]["detector"] == "credit_card"
    assert "ALTERED" in result["redaction_note"]
    assert "description" in result["redaction_note"]


def test_annotate_is_a_no_op_on_a_clean_write():
    """The overwhelmingly common case must not grow keys — an agent reading
    every write result pays for them."""
    assert annotate({"slug": "tk-x"}) == {"slug": "tk-x"}


def test_annotate_passes_none_through():
    """`None` is the "no such row" convention; there is nothing to attach to."""
    assert annotate(None) is None


# ── end to end, through the real write path ──────────────────────────────────


@pytest.fixture
def guarded_server(server, monkeypatch):
    """``server`` builds its client WITHOUT a guard, so scanning is inert there.

    Rebinding the SAME client with a guard is what makes these tests exercise
    the wiring under test — the unit tests above prove the guard records, and
    this proves the recording survives all the way to what a caller reads.
    """
    from witan import server as srv

    monkeypatch.setattr(
        srv.client,
        "guard",
        write_guard_from_config(ScanConfig(enabled=True, pii_action="redact")),
    )
    return server


@requires_omnigraph
def test_memory_store_tells_the_caller_its_content_was_rewritten(guarded_server):
    """The original defect, end to end: this returned a clean success and the
    numbers were simply gone from the record."""
    result = guarded_server.memory_store(
        kind="agent_context",
        title="probe durations",
        content=f"latencies {CARD} seconds",
    )

    assert "redaction_note" in result, "the write was altered and said nothing"
    assert result["redactions"][0]["field"] == "content"
    assert result["redactions"][0]["detector"] == "credit_card"

    stored = guarded_server.memory_get(result["slug"])
    assert stored["content"] == "latencies «redacted:credit_card» seconds"


@requires_omnigraph
def test_a_clean_memory_store_carries_no_redaction_keys(guarded_server):
    result = guarded_server.memory_store(
        kind="agent_context", title="clean", content="nothing sensitive"
    )
    assert "redaction_note" not in result
    assert "redactions" not in result


@requires_omnigraph
def test_task_update_tells_the_caller_its_description_was_rewritten(guarded_server):
    """The exact call that lost the measurements."""
    created = guarded_server.task_create(title="measure", description="tbd")
    updated = guarded_server.task_update(
        slug=created["slug"], description=f"durations {CARD} observed"
    )
    assert "redaction_note" in updated
    assert updated["redactions"][0]["field"] == "description"
