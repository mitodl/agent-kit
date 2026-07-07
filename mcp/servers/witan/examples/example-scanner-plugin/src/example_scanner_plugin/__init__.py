"""Example third-party witan.scanners plugin (ADR 0001 §D2).

Demonstrates the minimal contract a plugin must satisfy — no dependency on
``witan`` itself, since ``witan.scan.Scanner`` is a structural (duck-typed)
``Protocol`` and a finding only needs to expose a handful of attributes.

Detects "ACME Corp"'s internal employee-id format (``ACME-EMP-######``), a
company-specific identifier the built-in PII detectors don't know about —
the kind of org-specific rule this extension point exists for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_EMPLOYEE_ID_RE = re.compile(r"\bACME-EMP-\d{6}\b")

# Mirrors witan.scan.models.Category/Severity and witan.config.ScanAction —
# copied rather than imported (see module docstring), but kept exact so a
# plugin author can't accidentally return a value enforcement doesn't
# recognize (e.g. an unsupported `action` silently falling through to warn).
Category = Literal["secret", "pii"]
Severity = Literal["low", "medium", "high", "critical"]
Action = Literal["block", "redact", "warn"]


@dataclass(frozen=True)
class Finding:
    """Mirrors the attributes ``WriteGuard`` reads off a finding.

    A real plugin may instead depend on ``witan`` and construct
    ``witan.scan.Finding`` directly (it has the same fields plus validation) —
    this dataclass exists only to prove the contract doesn't require that
    dependency.
    """

    detector: str
    category: Category
    start: int
    end: int
    severity: Severity = "high"
    preview: str = ""
    action: Action | None = None


class AcmeEmployeeIdScanner:
    """Flags ACME Corp employee ids (e.g. ``ACME-EMP-123456``) as PII."""

    name = "acme_employee_id"
    category = "pii"

    def scan(self, text: str, field: str, node_type: str) -> list[Finding]:
        return [
            Finding(
                detector=self.name,
                category=self.category,
                start=match.start(),
                end=match.end(),
                preview=f"<{self.name}: {match.end() - match.start()} chars>",
            )
            for match in _EMPLOYEE_ID_RE.finditer(text)
        ]
