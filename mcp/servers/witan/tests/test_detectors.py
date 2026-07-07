"""Tests for the built-in secret and PII detectors."""

import pytest

from witan.config import ScanConfig
from witan.scan import WriteBlocked, write_guard_from_config
from witan.scan.detectors import default_scanners

SCANNERS = {s.name: s for s in default_scanners()}


def run(name, text):
    return SCANNERS[name].scan(text, "content", "Memory")


def matched(name, text):
    """The substrings the named detector flags in text."""
    return [text[f.start : f.end] for f in run(name, text)]


# ── rule set sanity ─────────────────────────────────────────────────────────────


def test_detector_names_are_unique():
    names = [s.name for s in default_scanners()]
    assert len(names) == len(set(names))


def test_every_finding_carries_its_detector_and_no_raw_value():
    findings = run("aws_access_key", "key AKIAIOSFODNN7EXAMPLE here")
    assert findings[0].detector == "aws_access_key"
    assert "AKIA" not in findings[0].preview  # preview is secret-free


# ── secret detectors ────────────────────────────────────────────────────────────


# These are synthetic test fixtures, not real credentials. The secret scanners
# are told so explicitly: `# pragma: allowlist secret` (honored by GitGuardian in
# CI) plus `gitleaks:allow` for the local gitleaks hook; detect-private-key has no
# inline pragma, so this file is excluded from it in prek.toml.
@pytest.mark.parametrize(
    "name,sample",
    [
        # Value + pragma share one line so both scanners see the marker.
        ("aws_access_key", "AKIAIOSFODNN7EXAMPLE"),  # pragma: allowlist secret gitleaks:allow
        ("github_token", "ghp_" + "a" * 36),
        ("github_token", "github_pat_" + "b" * 60),
        ("slack_token", "xoxb-123456789012-abcdef"),  # pragma: allowlist secret gitleaks:allow
        ("google_api_key", "AIza" + "C" * 35),
        ("private_key_block", "-----BEGIN RSA PRIVATE KEY-----"),  # pragma: allowlist secret gitleaks:allow
        ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N"),  # pragma: allowlist secret gitleaks:allow
    ],
)  # fmt: skip
def test_secret_detector_matches(name, sample):
    assert sample in matched(name, f"prefix {sample} suffix")


def test_secret_assignment_reports_only_the_value():
    assert matched("secret_assignment", "password = hunter2xyz") == ["hunter2xyz"]
    assert matched("secret_assignment", 'api_key: "s3cr3ttoken99"') == ["s3cr3ttoken99"]


def test_secret_assignment_skips_placeholders():
    assert run("secret_assignment", "password = changeme") == []
    assert run("secret_assignment", "token = xxxxxxxx") == []


def test_aws_key_requires_full_shape():
    assert run("aws_access_key", "AKIA123") == []  # too short


# ── entropy ─────────────────────────────────────────────────────────────────────


def test_entropy_flags_random_token():
    assert matched("high_entropy_string", "tok aB3xK9mZ2qP7wL5rT8vC1nY6dF4hG0jS end")


def test_entropy_spares_git_sha_and_uuid():
    sha = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"  # 40 hex, entropy ~4.0
    uuid = "550e8400-e29b-41d4-a716-446655440000"
    assert run("high_entropy_string", sha) == []
    assert run("high_entropy_string", uuid) == []


def test_entropy_spares_low_entropy_repeats():
    assert run("high_entropy_string", "a" * 40) == []


# ── PII detectors ───────────────────────────────────────────────────────────────


def test_email_detector():
    assert matched("email", "ping alice@example.com now") == ["alice@example.com"]
    assert run("email", "no address here") == []


def test_phone_requires_separators():
    assert matched("phone", "call 415-555-2671 today") == ["415-555-2671"]
    assert matched("phone", "+1 415 555 2671") == ["+1 415 555 2671"]
    assert run("phone", "id 4155552671") == []  # bare digits are not a phone


def test_ssn_requires_dashes():
    assert matched("us_ssn", "ssn 123-45-6789") == ["123-45-6789"]
    assert run("us_ssn", "123456789") == []


def test_credit_card_luhn_validated():
    assert matched("credit_card", "card 4111 1111 1111 1111 end") == [
        "4111 1111 1111 1111"
    ]
    assert run("credit_card", "num 4111 1111 1111 1112") == []  # fails Luhn


# ── end-to-end through the real guard ────────────────────────────────────────────


def _guard():
    return write_guard_from_config(ScanConfig(enabled=True))


def test_guard_blocks_real_secret():
    with pytest.raises(WriteBlocked):
        _guard()("insert_memory", {"title": "t", "content": "key AKIAIOSFODNN7EXAMPLE"})


def test_guard_redacts_real_pii():
    out = _guard()(
        "insert_task", {"title": "t", "description": "mail me alice@x.com ok"}
    )
    assert "alice@x.com" not in out["description"]
    assert "redacted:email" in out["description"]


def test_guard_allows_git_sha():
    """A commit SHA in a task body must not trip the secret guard."""
    params = {
        "title": "t",
        "description": "fixed in a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
    }
    assert _guard()("insert_task", params) == params
