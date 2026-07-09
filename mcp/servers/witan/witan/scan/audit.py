"""Structured audit events for scan findings (ADR 0001 §D3, tk-audit-logging).

Every finding a scanner produces — whether it ends up blocking a write,
redacting a field, or just warning — gets exactly one audit event, emitted as
a single structured log line via the standard :mod:`logging` module. That
sink beats the alternatives considered: a graph ``ScanEvent`` node would
itself become sensitive-adjacent state to secure and retention-manage, and a
bare metrics counter would lose the per-finding detail an operator needs to
debug a policy or a false positive. A log line composes with whatever the
deployment already aggregates logs into (Loki, CloudWatch, …).

The event never carries the matched value — only what :class:`~.models.Finding`
already exposes (detector, category, severity, its secret-free ``preview``)
plus where the write was headed (query, node type, field) and the node's
``slug`` when the mutation has one, so occurrences can be correlated without
touching content.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ..config import ScanAction
from .allowlist import SuppressionReason
from .models import Category, Finding, Severity

logger = logging.getLogger("witan.scan.audit")

AuditOutcome = Literal["blocked", "redacted", "warned", "suppressed"]

_OUTCOME_FOR_ACTION: dict[ScanAction, AuditOutcome] = {
    "block": "blocked",
    "redact": "redacted",
    "warn": "warned",
}


class AuditEvent(BaseModel):
    """One scan finding's disposition on a single write. Secret-free.

    ``action`` is the *effective* action applied to this finding on this
    write — already downgraded to ``"warn"`` when an allowlist mechanism
    suppressed it, so it reflects what happened, not the category/override
    policy that would otherwise apply (that policy is reconstructable from
    ``ScanConfig`` if needed; it's not duplicated here). ``outcome`` is what
    actually happened to the *write* — always ``"blocked"`` when any finding
    on the write (this one or another field's) caused a block, since nothing
    is persisted in that case regardless of what any individual finding's own
    action was; ``"suppressed"`` when an allowlist mechanism downgraded this
    finding to audit-only, in which case ``suppressed_by`` names how.
    """

    model_config = ConfigDict(frozen=True)

    query_name: str
    node_type: str
    field: str
    slug: str | None
    detector: str
    category: Category
    severity: Severity
    action: ScanAction
    outcome: AuditOutcome
    preview: str
    suppressed_by: SuppressionReason | None = None


def emit(
    *,
    query_name: str,
    node_type: str,
    field: str,
    slug: str | None,
    finding: Finding,
    action: ScanAction,
    write_blocked: bool = False,
    suppressed_by: SuppressionReason | None = None,
) -> None:
    """Log one structured audit line for a single finding's disposition.

    ``write_blocked`` must reflect the *overall* write's fate, not just this
    finding's own action — pass ``True`` for every finding on a write that
    ends up rejected, even ones whose own action resolved to redact/warn,
    since none of them were actually redacted/warned into the store.

    ``suppressed_by`` takes precedence over ``write_blocked``/``action``: an
    allowlisted finding never blocks or redacts (see ``WriteGuard``), so if
    it's set the outcome is always ``"suppressed"``.
    """
    outcome: AuditOutcome
    if suppressed_by is not None:
        outcome = "suppressed"
    elif write_blocked:
        outcome = "blocked"
    else:
        outcome = _OUTCOME_FOR_ACTION[action]
    event = AuditEvent(
        query_name=query_name,
        node_type=node_type,
        field=field,
        slug=slug,
        detector=finding.detector,
        category=finding.category,
        severity=finding.severity,
        action=action,
        outcome=outcome,
        preview=finding.preview,
        suppressed_by=suppressed_by,
    )
    logger.info(
        "witan scan: %s %s on %s.%s (%s)",
        event.outcome,
        event.detector,
        event.node_type,
        event.field,
        event.preview,
        extra={"scan_audit": event.model_dump()},
    )
