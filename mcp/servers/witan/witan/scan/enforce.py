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
from .models import Finding, ScannerError
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

        blocked: list[tuple[str, Finding]] = []
        redactions: dict[str, str] = {}
        for field in fields:
            value = params.get(field)
            if not isinstance(value, str) or not value:
                continue
            findings = self._scan(value, field, node_type)
            if not findings:
                continue
            field_blocked, redacted = self._apply(field, value, findings)
            blocked.extend(field_blocked)
            if redacted is not None:
                redactions[field] = redacted

        if blocked:
            raise WriteBlocked(query_name, blocked)
        if redactions:
            return {**params, **redactions}
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

    def _apply(
        self, field: str, value: str, findings: list[Finding]
    ) -> tuple[list[tuple[str, Finding]], str | None]:
        blocked: list[tuple[str, Finding]] = []
        to_redact: list[Finding] = []
        for finding in findings:
            action = finding.action or self._action_for(finding.category)
            if action == "block":
                blocked.append((field, finding))
            elif action == "redact":
                to_redact.append(finding)
                logger.info(
                    "witan scan: redacting %s in %s (%s)",
                    finding.detector,
                    field,
                    finding.preview,
                )
            else:  # warn
                logger.warning(
                    "witan scan: %s in %s (%s) stored unredacted (warn)",
                    finding.detector,
                    field,
                    finding.preview,
                )
        if blocked:
            return blocked, None  # write will be rejected; don't bother redacting
        return blocked, (_redact(value, to_redact) if to_redact else None)

    def _action_for(self, category: str) -> ScanAction:
        return (
            self._config.pii_action if category == "pii" else self._config.secret_action
        )


def _redact(value: str, findings: list[Finding]) -> str:
    """Replace each finding span with a placeholder, merging overlaps.

    Minimal but correct: the fuller redaction engine (node flagging, salted
    hashes) is a separate task; this is what interception needs to function.
    """
    spans = sorted((f.start, f.end, f.detector) for f in findings)
    merged: list[list] = []
    for start, end, detector in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end, detector])
    out = value
    for start, end, detector in reversed(merged):
        out = f"{out[:start]}«redacted:{detector}»{out[end:]}"
    return out


def write_guard_from_config(config: ScanConfig) -> WriteGuard | None:
    """Build the guard, or None when scanning is disabled.

    Returning None keeps the write path completely untouched (and skips loading
    any plugins) unless scanning is explicitly enabled.
    """
    if not config.enabled:
        return None
    return WriteGuard(config, ScannerRegistry.from_config(config))
