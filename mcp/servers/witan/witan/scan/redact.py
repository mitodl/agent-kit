"""Span redaction + node flagging (ADR 0001 §D3, tk-redaction-engine-node-flagging).

Two small, independent pieces used by :class:`~witan.scan.enforce.WriteGuard`
once it has decided a field's findings should be redacted rather than blocked:

- :func:`redact_spans` rewrites the value, replacing each finding's span with a
  stable, non-reversible placeholder. The placeholder names only the detector,
  never the matched text, and is itself inert — re-scanning it produces no
  further findings, so redacting an already-redacted value is a no-op.
- :func:`flag_redacted` records, on the node itself, that *some* redaction
  happened — via the existing ``tags`` list, so no schema change is needed.
  It deliberately carries no category/detector detail (that belongs to audit
  logging, a separate concern) — just a stable marker for "this content was
  altered before storage."
"""

from __future__ import annotations

from .models import Finding

REDACTED_TAG = "scan:redacted"


def redact_spans(value: str, findings: list[Finding]) -> str:
    """Replace each finding's span with ``«redacted:<detector>»``, merging
    overlapping/adjacent spans so no partial match survives."""
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


def flag_redacted(params: dict) -> dict:
    """Add :data:`REDACTED_TAG` to ``params['tags']`` if that key is present.

    Best-effort: mutations that don't carry a ``tags`` param (e.g. narrow
    single-field updates like ``update_workflow_project_description``) are
    left untouched rather than forced into a shape they don't support.
    """
    if "tags" not in params:
        return params
    raw_tags = params.get("tags")
    if raw_tags is None:
        tags: list = []
    elif isinstance(raw_tags, str):
        tags = [raw_tags]  # a bare string isn't the [String]? the schema expects
    else:
        tags = list(raw_tags)
    if REDACTED_TAG in tags:
        return params
    return {**params, "tags": [*tags, REDACTED_TAG]}
