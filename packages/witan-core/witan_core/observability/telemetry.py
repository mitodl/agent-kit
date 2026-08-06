"""OpenTelemetry tracing and metrics for the witan servers.

Ported from ``mitol.observability.telemetry`` (ol-django), with two deliberate
differences.

**Configuration comes from the standard OTel environment variables, not from
application settings.** ``OTEL_EXPORTER_OTLP_ENDPOINT``, ``OTEL_SERVICE_NAME``,
``OTEL_RESOURCE_ATTRIBUTES``, ``OTEL_TRACES_SAMPLER`` and friends are read by the
SDK itself, so this module passes no endpoint to the exporters and no service
name to the resource. That is what makes the deployed configuration — which sets
exactly those variables, pointed at the in-cluster Alloy receiver — work without
a witan-specific translation layer in between.

**Metrics are configured, which the reference package does not do.** The shared
service needs per-actor admission-cap and denial counters that a trace cannot
answer, so a ``MeterProvider`` is installed alongside the tracer.

Telemetry is never allowed to take the server down: a misconfigured endpoint or
a missing exporter package degrades to a warning and an un-instrumented process.
"""

from __future__ import annotations

import importlib.metadata
import os
from typing import Any

from witan_core.observability.logging import get_logger

log = get_logger(__name__)

_tracing_configured = False
_metrics_configured = False
_instrumented = False


def _endpoint() -> str | None:
    """The configured OTLP endpoint, if any.

    Both the generic and the signal-specific variables count: a deployment that
    only sets ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` still wants tracing on.
    """
    return (
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
    )


def _resource() -> Any:
    """The OTel resource, SDK defaults plus Kubernetes Downward API attributes.

    ``Resource.create`` already merges ``OTEL_SERVICE_NAME`` and
    ``OTEL_RESOURCE_ATTRIBUTES``; only the pod-identifying attributes, which the
    Downward API supplies under different names, have to be added by hand. They
    are what makes a span attributable to one replica when several are serving.
    """
    from opentelemetry.sdk.resources import Resource

    attributes = {
        key: value
        for key, value in {
            "k8s.pod.name": os.environ.get("KUBERNETES_POD_NAME"),
            "k8s.namespace.name": os.environ.get("KUBERNETES_NAMESPACE"),
            "k8s.node.name": os.environ.get("KUBERNETES_NODE_NAME"),
        }.items()
        if value
    }
    return Resource.create(attributes)


def _use_grpc() -> bool:
    """Whether to export over gRPC rather than HTTP.

    The house convention is ``http/protobuf`` (that is what the Alloy receiver is
    configured for on :4318), so HTTP is the default and gRPC is opt-in.
    """
    return os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf") == "grpc"


def configure_tracing() -> Any | None:
    """Install a ``TracerProvider``. Returns it, or ``None`` when not configured.

    With no endpoint set — every local CLI run, every stdio session — no provider
    is installed at all. The OTel API's default no-op tracer then makes every
    span in the codebase free, so instrumentation can be written unconditionally.
    """
    global _tracing_configured  # noqa: PLW0603 - module-level once-only guard
    if _tracing_configured:
        return None
    if not _endpoint():
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.baggage.propagation import W3CBaggagePropagator
        from opentelemetry.propagate import set_global_textmap
        from opentelemetry.propagators.composite import CompositePropagator
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.trace.propagation.tracecontext import (
            TraceContextTextMapPropagator,
        )

        if _use_grpc():
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

        # W3C traceparent in and out, so a trace started by the caller — an
        # agent, or the vMCP in front of us — continues here instead of starting
        # a second disconnected one.
        set_global_textmap(
            CompositePropagator(
                [TraceContextTextMapPropagator(), W3CBaggagePropagator()]
            )
        )

        provider = TracerProvider(resource=_resource())
        # No endpoint argument: the exporter reads OTEL_EXPORTER_OTLP_* itself.
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
    except Exception:  # noqa: BLE001 - telemetry must never be fatal
        log.warning("otel.tracing_setup_failed", exc_info=True)
        return None
    _tracing_configured = True
    return provider


def configure_metrics() -> Any | None:
    """Install a ``MeterProvider``. Returns it, or ``None`` when not configured.

    Beyond the reference package. The shared tier's open questions — how often
    per-actor admission caps are hit, how many distinct actors are writing at
    once, the Cedar denial rate — are all counters, and reconstructing counters
    by scraping log lines is exactly the fragility this module exists to remove.
    """
    global _metrics_configured  # noqa: PLW0603 - module-level once-only guard
    if _metrics_configured:
        return None
    if not _endpoint():
        return None
    try:
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        if _use_grpc():
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                OTLPMetricExporter,
            )
        else:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                OTLPMetricExporter,
            )

        reader = PeriodicExportingMetricReader(OTLPMetricExporter())
        provider = MeterProvider(resource=_resource(), metric_readers=[reader])
        metrics.set_meter_provider(provider)
    except Exception:  # noqa: BLE001 - telemetry must never be fatal
        log.warning("otel.metrics_setup_failed", exc_info=True)
        return None
    _metrics_configured = True
    return provider


def auto_instrument() -> None:
    """Apply every installed OTel instrumentor.

    Enumerating the entry-point group rather than hardcoding a list means adding
    ``opentelemetry-instrumentation-httpx`` to the image is enough to get HTTP
    client spans, with no code change here. ``OTEL_PYTHON_DISABLED_INSTRUMENTATIONS``
    is the standard escape hatch for one that misbehaves.
    """
    global _instrumented  # noqa: PLW0603 - module-level once-only guard
    if _instrumented:
        return
    skip = {
        name.strip()
        for name in os.environ.get("OTEL_PYTHON_DISABLED_INSTRUMENTATIONS", "").split(
            ","
        )
        if name.strip()
    }
    for entry_point in importlib.metadata.entry_points(
        group="opentelemetry_instrumentor"
    ):
        if entry_point.name in skip:
            continue
        try:
            entry_point.load()().instrument()
        except Exception:  # noqa: BLE001 - one bad instrumentor must not stop the rest
            log.warning(
                "otel.instrumentor_failed", instrumentor=entry_point.name, exc_info=True
            )
    _instrumented = True


def reset_telemetry() -> None:
    """Clear the once-only guards and the OTel globals. For tests.

    Clearing our own guards is not enough. ``set_tracer_provider`` /
    ``set_meter_provider`` install process-wide singletons that outlive the test
    that created them, and OTel offers no public way to take them back — so a
    test that shuts its provider down leaves a *shut-down* global behind, and the
    next ``get_meter`` anywhere in the session logs "A shutdown MeterProvider can
    not provide a Meter" onto stderr. That lands in the middle of an unrelated
    test's captured output and breaks it. Reaching for the private globals is
    what the OTel SDK's own test suite does for the same reason.
    """
    global _tracing_configured, _metrics_configured, _instrumented  # noqa: PLW0603
    _tracing_configured = False
    _metrics_configured = False
    _instrumented = False
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.util import _once
    except ImportError:  # pragma: no cover - requires the `observability` extra
        return
    trace._TRACER_PROVIDER = None  # noqa: SLF001
    trace._TRACER_PROVIDER_SET_ONCE = _once.Once()  # noqa: SLF001
    metrics._internal._METER_PROVIDER = None  # noqa: SLF001
    metrics._internal._METER_PROVIDER_SET_ONCE = _once.Once()  # noqa: SLF001
