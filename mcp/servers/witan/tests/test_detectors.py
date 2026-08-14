"""Tests for the built-in secret and PII detectors."""

import time

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


@pytest.mark.parametrize(
    "sample",
    [
        "4111111111111111",  # contiguous
        "4111-1111-1111-1111",  # dashed 4-4-4-4
        "3782 822463 10005",  # Amex 4-6-5
        "3056 930902 5904",  # Diners 4-6-4
        "3056 9309 0259 04",  # Diners 4-4-4-2
        # 13-digit Visa, 4-4-4-1. A continuation floor of 3 digits dropped this
        # and the Diners 4-4-4-2 above — both Luhn-valid, both matched by the
        # rule the grouping pattern replaced. Narrowing false positives must not
        # open a detection hole.
        "4222 2222 2222 2",
    ],
)
def test_credit_card_still_catches_every_printed_grouping(sample):
    """The grouping rule below must not cost real coverage — these are the ways
    a card is actually written."""
    assert matched("credit_card", f"card {sample} end") == [sample]


@pytest.mark.parametrize(
    "sample",
    [
        # The ORIGINAL loss: server-side handler durations from a task_update.
        # 15..29 is 18 digits once the spaces come out, and Luhn-valid.
        "3 3 5 6 8 8 10 10 11 13 13 15 17 18 19 20 22 25 27 29 31 33 36",
        "15 17 18 19 20 22 25 27 29",
        "ports 8080 8443 9090 3000 5432",
        "output_rows=1045 iops=3095 requests=3095 bytes_read=1095907",
    ],
)
def test_credit_card_does_not_eat_a_table_of_numbers(sample):
    """A run of small numbers is not card-shaped, whatever Luhn says about it.

    Luhn is a transcription checksum with a 1-in-10 hit rate on arbitrary
    digits, so under the old `\\b\\d(?:[ -]?\\d){12,18}\\b` roughly one
    measurement table in ten was silently rewritten — and measurement is
    exactly the content it hurts most to lose
    (tk-write-path-redaction-silently-rewrites-content-a-aec2b6).
    """
    assert run("credit_card", sample) == []


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


# ── false-positive corpus ─────────────────────────────────────────────────────
#
# Realistic memory/task/code content that must NOT trip any built-in detector.
# Each entry failing here is a false positive that would block or mangle a
# perfectly ordinary write.

FALSE_POSITIVE_CORPUS = [
    "Fixed in a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0 per review feedback.",
    "See docs/adr/0001-write-path-content-scanning.md for the design.",
    "Bumped tree-sitter to >=0.26,<0.27 in pyproject.toml.",
    "Node ids are UUIDv4, e.g. 550e8400-e29b-41d4-a716-446655440000.",
    "config.py:39-122 mirrors the RankConfig pattern.",
    "docker image tag: registry.example.com/witan:2026.07.07-a1b2c3d",
    "def scan(self, text: str, field: str, node_type: str) -> list[Finding]: ...",
    "WITAN_SCAN_ENABLED=true and WITAN_SCAN_SECRET_ACTION=block are the env knobs.",
    "PR #70 merged the write-path scanning skeleton.",
    "curl -s https://api.github.com/repos/mitodl/agent-kit/pulls/70",
    "The retry loop sleeps 0.05 * (attempt + 1) seconds between attempts.",
]


@pytest.mark.parametrize("text", FALSE_POSITIVE_CORPUS)
def test_false_positive_corpus_is_clean(text):
    findings = [
        f
        for scanner in default_scanners()
        for f in scanner.scan(text, "content", "Memory")
    ]
    assert findings == [], f"unexpected finding(s) in {text!r}: {findings}"


# ── perf guardrail ────────────────────────────────────────────────────────────


@pytest.mark.perf
def test_scan_latency_stays_bounded():
    """Content scanning must not become the write path's bottleneck. Loose
    bound (not a tight benchmark, and generous enough to survive a slow or
    contended CI runner) to catch a real regression, e.g. an
    accidentally-quadratic detector. Deselect on constrained runners with
    ``-m 'not perf'``."""
    text = (
        "Investigated the flaky test failure caused by a race condition "
        "in the connection pool. "
    ) * 100  # a few KB of ordinary prose, no matches
    guard = _guard()
    start = time.perf_counter()
    for _ in range(20):
        guard("insert_task", {"title": "t", "description": text})
    elapsed = time.perf_counter() - start
    assert elapsed < 10.0, f"20 scans of {len(text)} chars took {elapsed:.3f}s"
