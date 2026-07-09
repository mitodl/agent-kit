"""False-positive suppression for write-path scanning (ADR 0001, "False-positive
management").

Three independent mechanisms, all downgrading a :class:`~.models.Finding` to
audit-only — the value is written unchanged and the finding still gets exactly
one audit event, just with outcome ``"suppressed"`` instead of whatever its
category's policy would otherwise have done:

- **Regex allowlist** (``config.allowlist``): the finding's own matched span —
  not the whole field value — is tested against each pattern with
  ``re.fullmatch``, so a pattern for one known-good value can't suppress a
  different, longer secret merely because it contains a familiar substring.
- **Inline pragma**: one or more ``witan: allow-secret`` (or ``witan:
  allow-secret:<detector>`` to scope it to one detector) markers at the very
  *end* of the field's value — an author's explicit "I know, ship it anyway"
  for this one write. Anchored to the tail deliberately: an unanchored search
  would let the phrase suppress scanning just by appearing anywhere, e.g. in
  a memory that quotes this very docstring.
- **Hash allowlist** (``config.allowlist_hashes``): the matched span, salted
  with ``config.allowlist_salt`` and SHA-256'd, checked against a list of
  approved digests — approve a specific known value (e.g. a fixture
  credential) without ever putting its plaintext in config.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from ..config import ScanConfig
from .models import Finding

SuppressionReason = Literal["regex", "pragma", "hash"]

_PRAGMA_TOKEN = r"witan:\s*allow-secret(?::(?P<detector>[\w-]+))?"
_PRAGMA_RE = re.compile(_PRAGMA_TOKEN, re.IGNORECASE)
# One or more tokens, back-to-back, ending exactly at the string's end — the
# tail, not a `finditer` over the whole value (see module docstring).
_PRAGMA_TAIL_RE = re.compile(rf"(?:{_PRAGMA_TOKEN}\s*)+$", re.IGNORECASE)


def compile_allowlist(patterns: list[str]) -> list[re.Pattern[str]]:
    """Compile ``config.allowlist`` once per :class:`~.enforce.WriteGuard`
    build, so a scan doesn't recompile the same patterns on every write.

    ``ScanConfig`` already validates every pattern compiles at load time; this
    never raises for a config that passed that check.
    """
    return [re.compile(p) for p in patterns]


def _pragma_suppresses(field_value: str, detector: str) -> bool:
    tail = _PRAGMA_TAIL_RE.search(field_value)
    if tail is None:
        return False
    for m in _PRAGMA_RE.finditer(tail.group()):
        scope = m.group("detector")
        if scope is None or scope.lower() == detector.lower():
            return True
    return False


def _regex_suppresses(span_text: str, allowlist: list[re.Pattern[str]]) -> bool:
    return any(p.fullmatch(span_text) for p in allowlist)


def _hash_suppresses(span_text: str, salt: str, hashes: list[str]) -> bool:
    if not salt or not hashes:
        return False
    digest = hashlib.sha256((salt + span_text).encode()).hexdigest()
    return digest in hashes


def suppression_reason(
    finding: Finding,
    field_value: str,
    config: ScanConfig,
    allowlist: list[re.Pattern[str]],
) -> SuppressionReason | None:
    """Why ``finding`` should be downgraded to audit-only, or ``None`` if it
    isn't suppressed. Checked in pragma → regex → hash order; the first hit
    wins (order only matters for which reason is reported)."""
    span_text = field_value[finding.start : finding.end]
    if _pragma_suppresses(field_value, finding.detector):
        return "pragma"
    if _regex_suppresses(span_text, allowlist):
        return "regex"
    if _hash_suppresses(span_text, config.allowlist_salt, config.allowlist_hashes):
        return "hash"
    return None
