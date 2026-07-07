"""Tests for the redaction engine + node flagging (tk-redaction-engine-node-flagging)."""

from witan.config import ScanConfig
from witan.scan import Finding, ScannerRegistry, WriteGuard
from witan.scan.detectors import default_scanners
from witan.scan.redact import REDACTED_TAG, flag_redacted, redact_spans


def _f(detector, start, end, category="secret"):
    return Finding(detector=detector, category=category, start=start, end=end)


# ── redact_spans ─────────────────────────────────────────────────────────────


def test_redact_spans_replaces_match():
    out = redact_spans("mail a@b.com now", [_f("email", 5, 12, "pii")])
    assert out == "mail «redacted:email» now"


def test_redact_spans_merges_overlapping():
    """Overlapping spans collapse into one placeholder, keeping the earliest
    detector's name (merge order is deterministic, not "last wins")."""
    out = redact_spans("XXYY", [_f("a", 0, 3), _f("b", 2, 4)])
    assert out == "«redacted:a»"


def test_redact_spans_merges_adjacent():
    out = redact_spans("XX-XX", [_f("tok", 0, 2), _f("tok", 3, 5)])
    assert out == "«redacted:tok»-«redacted:tok»"


def test_redact_spans_is_idempotent_against_rescanning():
    """The placeholder itself must not trip any built-in detector — redacting
    an already-redacted value must be a no-op."""
    scanners = {s.name: s for s in default_scanners()}
    sample = "key AKIAIOSFODNN7EXAMPLE here"  # pragma: allowlist secret gitleaks:allow
    out = redact_spans(sample, [_f("aws_access_key", 4, 24)])
    for scanner in scanners.values():
        assert scanner.scan(out, "content", "Memory") == []


# ── flag_redacted ────────────────────────────────────────────────────────────


def test_flag_redacted_adds_tag_when_tags_present():
    out = flag_redacted({"title": "t", "tags": ["existing"]})
    assert out["tags"] == ["existing", REDACTED_TAG]


def test_flag_redacted_handles_null_tags():
    out = flag_redacted({"title": "t", "tags": None})
    assert out["tags"] == [REDACTED_TAG]


def test_flag_redacted_does_not_duplicate():
    out = flag_redacted({"tags": [REDACTED_TAG]})
    assert out["tags"] == [REDACTED_TAG]


def test_flag_redacted_skips_params_without_tags_key():
    """update_workflow_project_description etc. have no tags param — must be
    passed through untouched, not raise or invent one."""
    params = {"slug": "s", "description": "d"}
    assert flag_redacted(params) == params


# ── end-to-end via WriteGuard ─────────────────────────────────────────────────


def _guard(**cfg_kw):
    cfg = ScanConfig(enabled=True, **cfg_kw)
    return WriteGuard(cfg, ScannerRegistry.from_config(cfg))


def test_guard_flags_node_when_tags_param_present():
    out = _guard()(
        "insert_task",
        {"title": "t", "description": "mail alice@x.com ok", "tags": ["ops"]},
    )
    assert REDACTED_TAG in out["tags"]


def test_guard_does_not_flag_query_without_tags_param():
    out = _guard()(
        "update_workflow_project_description",
        {"slug": "s", "description": "mail alice@x.com ok"},
    )
    assert "tags" not in out
