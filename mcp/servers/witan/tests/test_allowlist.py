"""Tests for false-positive suppression: regex allowlist, inline pragma, and
salted hash allowlist (witan/scan/allowlist.py)."""

import pytest

from witan.config import ScanConfig
from witan.scan.allowlist import compile_allowlist, suppression_reason
from witan.scan.models import Finding


def _finding(text: str, needle: str, **kw) -> Finding:
    start = text.index(needle)
    return Finding(
        detector=kw.pop("detector", "aws_key"),
        category=kw.pop("category", "secret"),
        start=start,
        end=start + len(needle),
        **kw,
    )


def _cfg(**kw) -> ScanConfig:
    return ScanConfig(**kw)


# ── regex allowlist ──────────────────────────────────────────────────────────


def test_regex_allowlist_suppresses_matching_span():
    text = "key AKIAIOSFODNN7EXAMPLE here"
    finding = _finding(text, "AKIAIOSFODNN7EXAMPLE")
    cfg = _cfg(allowlist=["AKIAIOSFODNN7EXAMPLE"])
    reason = suppression_reason(finding, text, cfg, compile_allowlist(cfg.allowlist))
    assert reason == "regex"


def test_regex_allowlist_requires_full_match_of_span():
    """A pattern that only matches part of the span must not suppress it —
    otherwise a known-good prefix could hide an unrelated real secret."""
    text = "key AKIAIOSFODNN7EXAMPLE here"
    finding = _finding(text, "AKIAIOSFODNN7EXAMPLE")
    cfg = _cfg(allowlist=["AKIA"])
    reason = suppression_reason(finding, text, cfg, compile_allowlist(cfg.allowlist))
    assert reason is None


def test_regex_allowlist_no_match_is_not_suppressed():
    text = "key AKIAIOSFODNN7EXAMPLE here"
    finding = _finding(text, "AKIAIOSFODNN7EXAMPLE")
    cfg = _cfg(allowlist=["some-other-pattern"])
    reason = suppression_reason(finding, text, cfg, compile_allowlist(cfg.allowlist))
    assert reason is None


def test_invalid_allowlist_regex_rejected_at_config_load():
    with pytest.raises(ValueError, match="invalid allowlist regex"):
        ScanConfig(allowlist=["(unclosed"])


# ── inline pragma ────────────────────────────────────────────────────────────


def test_bare_pragma_suppresses_every_detector():
    text = "here AKIA123 witan: allow-secret"
    finding = _finding(text, "AKIA123")
    cfg = _cfg()
    reason = suppression_reason(finding, text, cfg, [])
    assert reason == "pragma"


def test_scoped_pragma_suppresses_only_named_detector():
    text = "here AKIA123 witan: allow-secret:aws_key"
    finding = _finding(text, "AKIA123", detector="aws_key")
    cfg = _cfg()
    assert suppression_reason(finding, text, cfg, []) == "pragma"

    other = _finding(text, "AKIA123", detector="generic_token")
    assert suppression_reason(other, text, cfg, []) is None


def test_pragma_is_case_insensitive():
    text = "here AKIA123 WITAN: ALLOW-SECRET"
    finding = _finding(text, "AKIA123")
    assert suppression_reason(finding, text, _cfg(), []) == "pragma"


def test_no_pragma_is_not_suppressed():
    text = "here AKIA123 nothing special"
    finding = _finding(text, "AKIA123")
    assert suppression_reason(finding, text, _cfg(), []) is None


def test_pragma_mid_text_does_not_suppress():
    """The pragma must be anchored to the end of the value — a mid-text
    occurrence (e.g. a memory that quotes/describes this feature) must not
    accidentally suppress an unrelated finding later in the same value."""
    text = "witan: allow-secret is how you suppress a finding. AKIA123 leaked here"
    finding = _finding(text, "AKIA123")
    assert suppression_reason(finding, text, _cfg(), []) is None


def test_multiple_trailing_scoped_pragmas():
    text = "leaked AKIA123 and ghp_abc witan: allow-secret:aws_key witan: allow-secret:github_token"
    aws = _finding(text, "AKIA123", detector="aws_key")
    gh = _finding(text, "ghp_abc", detector="github_token")
    assert suppression_reason(aws, text, _cfg(), []) == "pragma"
    assert suppression_reason(gh, text, _cfg(), []) == "pragma"


def test_scoped_pragma_detector_match_is_case_insensitive():
    text = "here AKIA123 witan: allow-secret:AWS_KEY"
    finding = _finding(text, "AKIA123", detector="aws_key")
    assert suppression_reason(finding, text, _cfg(), []) == "pragma"


# ── hash allowlist ───────────────────────────────────────────────────────────


def test_hash_allowlist_suppresses_matching_digest():
    import hashlib

    text = "key AKIA123 here"
    finding = _finding(text, "AKIA123")
    salt = "pepper"
    digest = hashlib.sha256((salt + "AKIA123").encode()).hexdigest()
    cfg = _cfg(allowlist_salt=salt, allowlist_hashes=[digest])
    assert suppression_reason(finding, text, cfg, []) == "hash"


def test_hash_allowlist_wrong_digest_not_suppressed():
    text = "key AKIA123 here"
    finding = _finding(text, "AKIA123")
    cfg = _cfg(allowlist_salt="pepper", allowlist_hashes=["deadbeef"])
    assert suppression_reason(finding, text, cfg, []) is None


def test_hash_allowlist_without_salt_is_inert():
    """Digests with no salt configured never match — a deployment that forgets
    to set allowlist_salt gets a no-op, not an accidental wildcard suppress."""
    import hashlib

    text = "key AKIA123 here"
    finding = _finding(text, "AKIA123")
    digest = hashlib.sha256(("" + "AKIA123").encode()).hexdigest()
    cfg = _cfg(allowlist_hashes=[digest])  # no allowlist_salt
    assert suppression_reason(finding, text, cfg, []) is None


# ── precedence / no suppression ─────────────────────────────────────────────


def test_nothing_configured_is_not_suppressed():
    text = "key AKIA123 here"
    finding = _finding(text, "AKIA123")
    assert suppression_reason(finding, text, _cfg(), []) is None
