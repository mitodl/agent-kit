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

**Sentry is gated on its own variable, independent of OTel.** Alloy/Tempo
already have every span; Sentry's value-add is issue grouping and regression
alerting on top of the exceptions this process already logs, so it hooks the
same stdlib logging chain ``configure_logging`` terminates in rather than
standing up a second tracing pipeline.
"""

from __future__ import annotations

import importlib.metadata
import logging
import os
from typing import Any

from witan_core.observability.logging import get_logger

log = get_logger(__name__)

_instrumented = False
# The providers this module installed, or None. Held rather than tracked with a
# bare "already ran" flag so a repeat call can return the provider in effect:
# otherwise "already configured" and "not configured" are both None and a caller
# cannot tell them apart.
_tracer_provider: Any | None = None
_meter_provider: Any | None = None
_sentry_client: Any | None = None


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
    """Install a ``TracerProvider``. Returns the one in effect, or ``None``.

    Idempotent, and a repeat call returns the *same* provider rather than
    ``None`` — so the return value always answers "what is installed", not "did
    this particular call do the installing".

    ``None`` means genuinely no provider: with no endpoint set — every local CLI
    run, every stdio session — nothing is installed at all. The OTel API's
    default no-op tracer then makes every span in the codebase free, so
    instrumentation can be written unconditionally at the call sites.
    """
    global _tracer_provider  # noqa: PLW0603 - module-level singleton
    if _tracer_provider is not None:
        return _tracer_provider
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
    _tracer_provider = provider
    return provider


def configure_metrics() -> Any | None:
    """Install a ``MeterProvider``. Returns the one in effect, or ``None``.

    Same idempotency contract as :func:`configure_tracing`.

    Beyond the reference package. The shared tier's open questions — how often
    per-actor admission caps are hit, how many distinct actors are writing at
    once, the Cedar denial rate — are all counters, and reconstructing counters
    by scraping log lines is exactly the fragility this module exists to remove.
    """
    global _meter_provider  # noqa: PLW0603 - module-level singleton
    if _meter_provider is not None:
        return _meter_provider
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
    _meter_provider = provider
    return provider


def configure_sentry() -> Any | None:
    """Install the Sentry SDK. Returns the client in effect, or ``None``.

    Same idempotency and never-fatal contract as :func:`configure_tracing`:
    ``None`` means genuinely unconfigured (no ``SENTRY_DSN``, or a client that
    failed to install), and a repeat call returns the client already in effect
    rather than re-initializing.

    ``LoggingIntegration``'s default ``event_level`` (``ERROR``) is passed
    explicitly because the whole point of hooking it onto the stdlib chain
    ``configure_logging`` terminates in is that it does the filtering: the
    many ``exc_info=True`` calls throughout witan at DEBUG/INFO/WARNING for
    expected, already-handled failures stay exactly that — breadcrumbs, not
    Sentry issues — while an actual ``log.error``/``log.exception`` reaches
    Sentry with no separate ``capture_exception`` call needed at the site.
    """
    global _sentry_client  # noqa: PLW0603 - module-level singleton
    if _sentry_client is not None:
        return _sentry_client
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return None
    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration

        sentry_sdk.init(
            dsn=dsn,
            environment=os.environ.get("SENTRY_ENVIRONMENT")
            or os.environ.get("KUBERNETES_NAMESPACE"),
            release=os.environ.get("SENTRY_RELEASE"),
            # Tempo (via the OTel setup above) already carries every span;
            # Sentry's own tracing would just be a second, disconnected
            # sample of the same requests.
            traces_sample_rate=0.0,
            send_default_pii=False,
            integrations=[LoggingIntegration(event_level=logging.ERROR)],
        )
        client = sentry_sdk.get_client()
        if not client.is_active():
            # A malformed-but-not-raising config (e.g. a DSN string sentry_sdk
            # accepts syntactically but treats as inert) lands here rather than
            # in the except below.
            log.warning("sentry.setup_produced_inactive_client")
            return None
    except Exception:  # noqa: BLE001 - telemetry must never be fatal
        log.warning("sentry.setup_failed", exc_info=True)
        return None
    _sentry_client = client
    return client


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
    """Clear the once-only guards and the OTel/Sentry globals. For tests.

    Clearing our own guards is not enough. ``set_tracer_provider`` /
    ``set_meter_provider`` install process-wide singletons that outlive the test
    that created them, and OTel offers no public way to take them back — so a
    test that shuts its provider down leaves a *shut-down* global behind, and the
    next ``get_meter`` anywhere in the session logs "A shutdown MeterProvider can
    not provide a Meter" onto stderr. That lands in the middle of an unrelated
    test's captured output and breaks it. Reaching for the private globals is
    what the OTel SDK's own test suite does for the same reason.

    Sentry's SDK carries an equivalent process-wide global (its "current
    scope"'s client), cleared the same way: through the public
    ``get_global_scope().set_client(None)`` rather than a private attribute,
    since the SDK exposes one.
    """
    global _tracer_provider, _meter_provider, _instrumented, _sentry_client  # noqa: PLW0603
    _tracer_provider = None
    _meter_provider = None
    _instrumented = False
    _sentry_client = None
    try:
        import sentry_sdk

        sentry_sdk.get_global_scope().set_client(None)
    except ImportError:  # pragma: no cover - requires the `sentry` extra
        pass
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.util import _once
    except ImportError:  # pragma: no cover - requires the `observability` extra
        return
    trace._TRACER_PROVIDER = None  # noqa: SLF001
    trace._TRACER_PROVIDER_SET_ONCE = _once.Once()  # noqa: SLF001
    metrics._internal._METER_PROVIDER = None  # noqa: SLF001
    metrics._internal._METER_PROVIDER_SET_ONCE = _once.Once()  # noqa: SLF001
