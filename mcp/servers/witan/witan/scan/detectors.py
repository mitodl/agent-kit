"""Built-in secret and PII detectors (ADR 0001).

Zero-dependency, high-signal regex + entropy rules. Each rule is its own
:class:`Scanner` with a unique ``name``, so ``disabled_detectors`` can switch off
any single rule (e.g. a noisy ``phone`` or ``high_entropy_string``) without
touching the others, and every ``Finding.detector`` is that rule's name.

Detection is best-effort — novel or obfuscated secrets will slip through, and the
entropy rule in particular trades some false positives for coverage. Thresholds
are tuned to spare common non-secrets (git SHAs, UUIDs) but the write path is
opt-in and every rule is individually disableable.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable

from .models import Category, Finding, Severity, masked_preview

# A validator turns a raw regex match into the span to report, or None to reject
# (e.g. Luhn failed, value is a placeholder). Default reports the whole match.
Validator = Callable[[re.Match], "tuple[int, int] | None"]


class RegexScanner:
    """A single named detection rule backed by one regular expression."""

    def __init__(
        self,
        name: str,
        category: Category,
        pattern: str,
        *,
        severity: Severity = "high",
        flags: int = 0,
        validate: Validator | None = None,
    ) -> None:
        self.name = name
        self.category = category
        self._re = re.compile(pattern, flags)
        self._severity = severity
        self._validate = validate

    def scan(self, text: str, field: str, node_type: str) -> list[Finding]:
        findings: list[Finding] = []
        for match in self._re.finditer(text):
            span = self._validate(match) if self._validate else match.span()
            if span is None:
                continue
            start, end = span
            findings.append(
                Finding(
                    detector=self.name,
                    category=self.category,
                    start=start,
                    end=end,
                    severity=self._severity,
                    preview=masked_preview(self.name, text[start:end]),
                )
            )
        return findings


# ── Shannon-entropy detector ────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_-]{20,}")


def _shannon_entropy(value: str) -> float:
    counts: dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(value)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


class EntropyScanner:
    """Flags long, high-entropy tokens that look like generated credentials.

    Default ``min_entropy`` of 4.5 bits/char spares pure-hex tokens (git SHAs,
    hex UUIDs max out near 4.0) while catching base64/base62 secrets (~5–6).
    """

    name = "high_entropy_string"
    category: Category = "secret"

    def __init__(
        self,
        *,
        min_length: int = 24,
        min_entropy: float = 4.5,
        severity: Severity = "medium",
    ) -> None:
        self._min_length = min_length
        self._min_entropy = min_entropy
        self._severity = severity

    def scan(self, text: str, field: str, node_type: str) -> list[Finding]:
        findings: list[Finding] = []
        for match in _TOKEN_RE.finditer(text):
            token = match.group()
            if len(token) < self._min_length:
                continue
            if _shannon_entropy(token) < self._min_entropy:
                continue
            findings.append(
                Finding(
                    detector=self.name,
                    category=self.category,
                    start=match.start(),
                    end=match.end(),
                    severity=self._severity,
                    preview=masked_preview(self.name, token),
                )
            )
        return findings


# ── Validators ──────────────────────────────────────────────────────────────────

_ASSIGNMENT_PLACEHOLDERS = frozenset(
    {
        "changeme",
        "password",
        "passwd",
        "secret",
        "example",
        "your_token",
        "your-token",
        "yourtoken",
        "none",
        "null",
        "redacted",
        "todo",
        "test",
        "xxxxxx",
        "placeholder",
        "dummy",
    }
)


def _validate_assignment(match: re.Match) -> tuple[int, int] | None:
    """Report only the value span of a `key = value` secret, skipping placeholders."""
    value = match.group("value")
    lowered = value.lower()
    if lowered in _ASSIGNMENT_PLACEHOLDERS:
        return None
    if set(value) <= {"*", "x", "X", ".", "-", "_"}:  # obviously masked
        return None
    return match.start("value"), match.end("value")


def _luhn_valid(digits: str) -> bool:
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _validate_credit_card(match: re.Match) -> tuple[int, int] | None:
    digits = re.sub(r"\D", "", match.group())
    if 13 <= len(digits) <= 19 and _luhn_valid(digits):
        return match.span()
    return None


# A card number is 13-19 digits written either as one run or in the groups a
# card is actually printed in: 4-4-4-4, Amex's 4-6-5, Diners' 4-6-4 and 4-4-4-2.
#
# ★ THE GROUPING IS THE WHOLE POINT, and it is what the first version of this
# pattern (`\b\d(?:[ -]?\d){12,18}\b`) left out. Allowing a separator between
# ANY two digits makes a card indistinguishable from a whitespace-separated
# table of small numbers, and Luhn does not break the tie: it is a
# transcription checksum with a 1-in-10 hit rate on arbitrary digits, not an
# identifier of card numbers. So roughly one measurement table in ten was
# silently rewritten to «redacted:credit_card». That is not hypothetical — a
# real `task_update` carrying handler latencies lost them exactly this way
# (tk-write-path-redaction-silently-rewrites-content-a-aec2b6), and the content
# most likely to hold long digit runs is measurement, which is the content it
# hurts most to lose.
#
# Requiring a 4-6 digit FIRST group and 3+ digit continuations costs nothing
# real: nobody writes a card as `4 111 1111 1111 1111`. Contiguous runs are
# unaffected, so an unformatted card is caught exactly as before.
#
# ★ THE SHORT TRAILING GROUP IS NOT AN AFTERTHOUGHT — it is two real cards.
# Diners is printed 4-4-4-2 and the 13-digit Visa is printed 4-4-4-1, so a
# continuation floor of 3 applied to the LAST group silently dropped both:
# `4222 2222 2222 2` is Luhn-valid, 13 digits, and was matched by the rule this
# replaces. Narrowing false positives must not open a detection hole, so the
# final group is allowed to be 1-2 digits. It cannot widen the false-positive
# class it was written to close: the 4-6 digit first group is what rejects a
# table of small numbers, and that is unchanged.
_CREDIT_CARD_PATTERN = (
    r"(?<!\d)(?:\d{13,19}|\d{4,6}(?:[ -]\d{3,6}){1,4}(?:[ -]\d{1,2})?)(?!\d)"
)


# ── Rule set ────────────────────────────────────────────────────────────────────


def default_scanners() -> list:
    """The full built-in detector set (secrets + PII), one Scanner per rule."""
    secrets = [
        RegexScanner(
            "aws_access_key", "secret", r"\b(?:AKIA|ASIA|AGPA|AROA)[0-9A-Z]{16}\b"
        ),
        RegexScanner(
            "github_token",
            "secret",
            r"\b(?:gh[posru]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b",
        ),
        RegexScanner("slack_token", "secret", r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"),
        RegexScanner("google_api_key", "secret", r"\bAIza[0-9A-Za-z_-]{35}\b"),
        RegexScanner(
            "private_key_block",
            "secret",
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----",
        ),
        RegexScanner(
            "jwt",
            "secret",
            r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",
        ),
        RegexScanner(
            "secret_assignment",
            "secret",
            r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|"
            r"auth[_-]?token|token)\b\s*[:=]\s*[\"']?(?P<value>[^\s\"']{8,})[\"']?",
            validate=_validate_assignment,
        ),
        EntropyScanner(),
    ]
    pii = [
        RegexScanner(
            "email",
            "pii",
            r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
            severity="medium",
        ),
        RegexScanner(
            "phone",
            "pii",
            r"(?<!\w)(?:\+?1[ .-])?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)",
            severity="medium",
        ),
        RegexScanner("us_ssn", "pii", r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
        RegexScanner(
            "credit_card",
            "pii",
            _CREDIT_CARD_PATTERN,
            validate=_validate_credit_card,
        ),
    ]
    return [*secrets, *pii]
