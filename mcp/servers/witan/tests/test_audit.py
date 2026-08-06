"""Tests for structured, secret-free audit events on scan findings."""

import logging

import pytest
from witan_core.observability import configure_logging, reset_logging

from witan.config import ScanConfig
from witan.scan import (
    Finding,
    ScannerRegistry,
    WriteBlocked,
    WriteGuard,
    masked_preview,
)
from witan.scan.audit import AuditEvent


@pytest.fixture(autouse=True)
def _structlog_through_stdlib():
    """Route structlog into stdlib logging so ``caplog`` can see audit events.

    ``audit.emit`` logs through structlog now. Only once ``configure_logging``
    has run does structlog use the stdlib ``LoggerFactory``; unconfigured it
    uses ``PrintLogger``, which writes straight to a stream and never creates a
    ``LogRecord`` for caplog to capture. Both serve paths call
    ``configure_observability()`` before handling anything, so configuring here
    reproduces production rather than working around it.
    """
    configure_logging(log_format="json", level="INFO", force=True)
    yield
    reset_logging()
    # dictConfig put a handler on the root logger; drop it so the next test's
    # caplog starts clean.
    logging.getLogger().handlers.clear()


def _audit(record):
    """The AuditEvent payload carried by one captured log record.

    ``audit.emit`` now logs through structlog, whose stdlib bridge puts the
    whole event dict in ``record.msg`` rather than in a bespoke ``scan_audit``
    attribute. The dict also carries pipeline keys (``event``, ``level``,
    ``logger``, ``timestamp``); those are filtered out here so these tests keep
    asserting on the audit payload instead of on pydantic's extra-field policy.
    """
    return {k: v for k, v in record.msg.items() if k in AuditEvent.model_fields}


class MatchScanner:
    def __init__(self, name, category, needle, *, severity="high"):
        self.name = name
        self.category = category
        self._needle = needle
        self._severity = severity

    def scan(self, text, field, node_type):
        start = text.find(self._needle)
        if start == -1:
            return []
        end = start + len(self._needle)
        return [
            Finding(
                detector=self.name,
                category=self.category,
                start=start,
                end=end,
                severity=self._severity,
                preview=masked_preview(self.name, self._needle),
            )
        ]


def _guard(scanners, **cfg_kw):
    return WriteGuard(ScanConfig(enabled=True, **cfg_kw), ScannerRegistry(scanners))


def test_block_emits_audit_event(caplog):
    caplog.set_level(logging.INFO, logger="witan.scan.audit")
    guard = _guard([MatchScanner("aws_key", "secret", "AKIASECRET")])
    with pytest.raises(WriteBlocked):
        guard(
            "insert_memory",
            {"slug": "mem-1", "title": "t", "content": "here AKIASECRET x"},
        )

    [record] = caplog.records
    event = AuditEvent(**_audit(record))
    assert event.outcome == "blocked"
    assert event.action == "block"
    assert event.detector == "aws_key"
    assert event.category == "secret"
    assert event.node_type == "Memory"
    assert event.field == "content"
    assert event.slug == "mem-1"
    assert "AKIASECRET" not in caplog.text


def test_redact_emits_audit_event(caplog):
    caplog.set_level(logging.INFO, logger="witan.scan.audit")
    guard = _guard([MatchScanner("email", "pii", "a@b.com")])
    guard("insert_memory", {"slug": "mem-2", "title": "t", "content": "a@b.com"})

    [record] = caplog.records
    event = AuditEvent(**_audit(record))
    assert event.outcome == "redacted"
    assert event.action == "redact"
    assert event.slug == "mem-2"
    assert "a@b.com" not in caplog.text


def test_warn_emits_audit_event(caplog):
    caplog.set_level(logging.INFO, logger="witan.scan.audit")
    guard = _guard([MatchScanner("aws_key", "secret", "AKIA")], secret_action="warn")
    guard("insert_task", {"slug": "task-1", "title": "t", "description": "AKIA stays"})

    [record] = caplog.records
    event = AuditEvent(**_audit(record))
    assert event.outcome == "warned"
    assert event.node_type == "Task"
    assert event.field == "description"
    assert "AKIA" not in caplog.text


def test_no_findings_emits_no_audit_event(caplog):
    caplog.set_level(logging.INFO, logger="witan.scan.audit")
    guard = _guard([MatchScanner("aws_key", "secret", "AKIA")])
    guard("insert_memory", {"slug": "mem-3", "title": "t", "content": "clean"})
    assert caplog.records == []


def test_slug_absent_is_none(caplog):
    caplog.set_level(logging.INFO, logger="witan.scan.audit")
    guard = _guard([MatchScanner("email", "pii", "a@b.com")])
    guard("insert_memory", {"title": "t", "content": "a@b.com"})

    [record] = caplog.records
    event = AuditEvent(**_audit(record))
    assert event.slug is None


def test_suppressed_finding_emits_suppressed_outcome(caplog):
    caplog.set_level(logging.INFO, logger="witan.scan.audit")
    guard = _guard([MatchScanner("aws_key", "secret", "AKIA")], allowlist=["AKIA"])
    guard("insert_memory", {"slug": "mem-6", "title": "t", "content": "AKIA here"})

    [record] = caplog.records
    event = AuditEvent(**_audit(record))
    assert event.outcome == "suppressed"
    assert event.suppressed_by == "regex"
    assert event.action == "warn"


def test_non_suppressed_finding_has_no_suppressed_by(caplog):
    caplog.set_level(logging.INFO, logger="witan.scan.audit")
    guard = _guard([MatchScanner("email", "pii", "a@b.com")])
    guard("insert_memory", {"slug": "mem-7", "title": "t", "content": "a@b.com"})

    [record] = caplog.records
    event = AuditEvent(**_audit(record))
    assert event.suppressed_by is None
    assert event.outcome == "redacted"


def test_block_in_one_field_marks_redact_in_another_field_as_blocked_too(caplog):
    """A write is all-or-nothing: if any field blocks, nothing is persisted,
    so every finding on the write must audit as "blocked" — not just the one
    that actually triggered it. Regression for a bug where a redact/warn
    finding processed before the blocking one was already logged as having
    succeeded, even though the whole write was then rejected."""
    caplog.set_level(logging.INFO, logger="witan.scan.audit")
    guard = _guard(
        [
            MatchScanner(
                "email", "pii", "a@b.com"
            ),  # would redact — title scanned first
            MatchScanner(
                "aws_key", "secret", "AKIA"
            ),  # blocks — content scanned second
        ]
    )
    with pytest.raises(WriteBlocked):
        guard("insert_memory", {"slug": "mem-4", "title": "a@b.com", "content": "AKIA"})

    events = {_audit(r)["field"]: AuditEvent(**_audit(r)) for r in caplog.records}
    assert events["title"].action == "redact"
    assert (
        events["title"].outcome == "blocked"
    )  # not "redacted" — nothing was persisted
    assert events["content"].action == "block"
    assert events["content"].outcome == "blocked"


def test_block_later_in_same_field_marks_earlier_redact_as_blocked_too(caplog):
    """Same regression, but both findings are in the same field/value."""
    caplog.set_level(logging.INFO, logger="witan.scan.audit")
    guard = _guard(
        [
            MatchScanner("email", "pii", "a@b.com"),
            MatchScanner("aws_key", "secret", "AKIA"),
        ]
    )
    with pytest.raises(WriteBlocked):
        guard(
            "insert_memory", {"slug": "mem-5", "title": "t", "content": "a@b.com AKIA"}
        )

    events = {_audit(r)["detector"]: AuditEvent(**_audit(r)) for r in caplog.records}
    assert events["email"].action == "redact"
    assert events["email"].outcome == "blocked"
    assert events["aws_key"].outcome == "blocked"
