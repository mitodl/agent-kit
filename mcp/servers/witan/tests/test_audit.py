"""Tests for structured, secret-free audit events on scan findings."""

import logging

import pytest

from witan.config import ScanConfig
from witan.scan import (
    Finding,
    ScannerRegistry,
    WriteBlocked,
    WriteGuard,
    masked_preview,
)
from witan.scan.audit import AuditEvent


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
    event = AuditEvent(**record.scan_audit)
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
    event = AuditEvent(**record.scan_audit)
    assert event.outcome == "redacted"
    assert event.action == "redact"
    assert event.slug == "mem-2"
    assert "a@b.com" not in caplog.text


def test_warn_emits_audit_event(caplog):
    caplog.set_level(logging.INFO, logger="witan.scan.audit")
    guard = _guard([MatchScanner("aws_key", "secret", "AKIA")], secret_action="warn")
    guard("insert_task", {"slug": "task-1", "title": "t", "description": "AKIA stays"})

    [record] = caplog.records
    event = AuditEvent(**record.scan_audit)
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
    event = AuditEvent(**record.scan_audit)
    assert event.slug is None
