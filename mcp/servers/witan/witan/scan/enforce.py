"""Write-path enforcement (ADR 0001 §D1/§D3).

A :class:`WriteGuard` is the callable wired into ``OmnigraphClient.change()``.
For a mutation it knows about, it scans the free-text params, then applies the
configured action: **block** (raise, rejecting the write), **redact** (rewrite
the param in place), or **warn** (log and pass through). It is the single point
that turns detector findings into policy, for every node type.
"""

from __future__ import annotations

import logging

from ..config import ScanAction, ScanConfig
from . import audit
from .models import Finding, ScannerError
from .redact import flag_redacted, redact_spans
from .registry import ScannerRegistry

logger = logging.getLogger("witan.scan")

# query_name → (node_type, scalar free-text params to scan). Only scalar string
# fields are scanned; list fields (tags, symbol_refs, files_changed, …) and
# structured/enum params are intentionally out of scope for now. Branch names
# (insert_code_branch) are deliberately excluded — witan never sanitises them.
# Keep this aligned with queries/mutations.gq when free-text fields are added.
FIELD_MAP: dict[str, tuple[str, tuple[str, ...]]] = {
    "insert_memory": ("Memory", ("title", "content")),
    "update_memory": ("Memory", ("title", "content")),
    "insert_topic": ("Topic", ("name",)),
    "insert_workflow_project": ("WorkflowProject", ("title", "description")),
    "update_workflow_project_description": ("WorkflowProject", ("description",)),
    "insert_workflow_session": ("WorkflowSession", ("summary",)),
    "update_workflow_session_end": ("WorkflowSession", ("summary",)),
    "insert_workflow_trace": ("WorkflowTrace", ("title", "description", "outcome")),
    "insert_task": ("Task", ("title", "description", "external_uri")),
    "update_task": ("Task", ("title", "description", "resolution", "external_uri")),
}


class WriteBlocked(RuntimeError):
    """Raised to reject a write whose content a scanner flagged for blocking.

    The message is **secret-free** — field name, detector id, and a masked
    preview only. The raw matched value never appears here or in any log line.
    """

    def __init__(self, query_name: str, findings: list[tuple[str, Finding]]) -> None:
        self.query_name = query_name
        self.findings = findings
        detail = "; ".join(
            f"{field}: {f.detector} {f.preview}" for field, f in findings
        )
        super().__init__(
            f"witan refused a {query_name} write — sensitive content detected "
            f"({detail}). Remove it and retry."
        )


class WriteGuard:
    """Scans and enforces on the free-text params of a single mutation."""

    def __init__(self, config: ScanConfig, registry: ScannerRegistry) -> None:
        self._config = config
        self._registry = registry

    def __call__(self, query_name: str, params: dict) -> dict:
        entry = FIELD_MAP.get(query_name)
        if entry is None:
            return params
        node_type, fields = entry
        slug = params.get("slug")

        # Resolve every field's findings to an action first, without emitting
        # audit events or mutating anything, so the write's fate (blocked or
        # not) is known before it's recorded — otherwise a block in field B
        # would leave field A's redact/warn findings audited as having
        # happened, when in fact nothing about this write was ever persisted.
        per_field: list[tuple[str, str, list[tuple[Finding, ScanAction]]]] = []
        blocked: list[tuple[str, Finding]] = []
        for field in fields:
            value = params.get(field)
            if not isinstance(value, str) or not value:
                continue
            findings = self._scan(value, field, node_type)
            if not findings:
                continue
            resolved = [(f, f.action or self._action_for(f.category)) for f in findings]
            per_field.append((field, value, resolved))
            blocked.extend((field, f) for f, a in resolved if a == "block")

        write_blocked = bool(blocked)
        redactions: dict[str, str] = {}
        for field, value, resolved in per_field:
            for finding, action in resolved:
                audit.emit(
                    query_name=query_name,
                    node_type=node_type,
                    field=field,
                    slug=slug,
                    finding=finding,
                    action=action,
                    write_blocked=write_blocked,
                )
            if not write_blocked:
                to_redact = [f for f, a in resolved if a == "redact"]
                if to_redact:
                    redactions[field] = redact_spans(value, to_redact)

        if write_blocked:
            raise WriteBlocked(query_name, blocked)
        if redactions:
            return flag_redacted({**params, **redactions})
        return params

    def _scan(self, value: str, field: str, node_type: str) -> list[Finding]:
        try:
            return self._registry.scan(value, field, node_type)
        except ScannerError as exc:
            if self._config.on_scanner_error == "warn":
                logger.warning(
                    "witan scan: scanner %r failed on %s.%s; allowing write "
                    "(on_scanner_error=warn)",
                    exc.scanner,
                    node_type,
                    field,
                )
                return []
            raise  # fail-closed: the ScannerError aborts the write

    def _action_for(self, category: str) -> ScanAction:
        return (
            self._config.pii_action if category == "pii" else self._config.secret_action
        )


def write_guard_from_config(config: ScanConfig) -> WriteGuard | None:
    """Build the guard, or None when scanning is disabled.

    Returning None keeps the write path completely untouched (and skips
    loading any plugins) when scanning is explicitly turned off — it's on by
    default.
    """
    if not config.enabled:
        return None
    return WriteGuard(config, ScannerRegistry.from_config(config))
