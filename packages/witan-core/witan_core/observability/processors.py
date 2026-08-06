"""structlog processors that enrich every event with ambient context.

Ported from ``mitol.observability.processors`` (ol-django). Both processors are
pure functions over the event dict with no framework coupling, which is why they
transfer verbatim from a Django app to an MCP server.

The OpenTelemetry import is guarded rather than required: log configuration is
useful on its own, and the ``observability`` extra's OTel half is dead weight for
a local stdio session that exports nothing. When OTel is absent the processor
degrades to a pass-through instead of the whole logging stack failing to import.
"""

from __future__ import annotations

import os
from typing import Any

try:
    from opentelemetry import trace
    from opentelemetry.trace import format_span_id, format_trace_id

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the no-OTel install
    _OTEL_AVAILABLE = False


def inject_otel_context(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Add ``trace_id``/``span_id`` so a log line joins its trace.

    This is done as a structlog processor rather than via OTel's
    ``LoggingInstrumentor`` so the ids land as real keys in the event dict — and
    therefore as real JSON fields — instead of being formatted into a message
    string that a log backend would have to parse back out.

    A non-recording or invalid span contributes nothing: a sampled-out span's ids
    correlate to a trace that was never exported, so emitting them would produce
    log lines linking to traces that do not exist.
    """
    if "trace_id" in event_dict and "span_id" in event_dict:
        return event_dict
    if not _OTEL_AVAILABLE:
        return event_dict
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return event_dict
    if not span.is_recording():
        return event_dict
    event_dict["trace_id"] = format_trace_id(ctx.trace_id)
    event_dict["span_id"] = format_span_id(ctx.span_id)
    return event_dict


# Read once at import: the Downward API values are fixed for the pod's lifetime,
# so re-reading os.environ on every log line would be pure overhead.
_K8S_CONTEXT: dict[str, str] = {
    key: value
    for key, value in {
        "pod_name": os.environ.get("KUBERNETES_POD_NAME"),
        "namespace": os.environ.get("KUBERNETES_NAMESPACE"),
        "node_name": os.environ.get("KUBERNETES_NODE_NAME"),
    }.items()
    if value
}


def inject_k8s_context(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Add pod/namespace/node when running under Kubernetes.

    Outside a cluster none of the Downward API variables are set, the dict is
    empty, and this is a no-op — so a developer's console output stays clean.
    """
    if _K8S_CONTEXT:
        event_dict.update(_K8S_CONTEXT)
    return event_dict
