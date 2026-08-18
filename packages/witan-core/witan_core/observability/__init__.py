"""Structured logging and OpenTelemetry for the witan servers.

Patterned after ``mitol-django-observability`` (ol-django ``src/observability/``)
so witan reports the way the rest of the estate does — same processor chain, same
JSON shape, same standard OTel environment variables — without inheriting the
Django coupling that package is built around.

One call sets everything up::

    from witan_core.observability import configure_observability

    configure_observability()

It is safe to call more than once and safe to call with nothing configured: with
no ``OTEL_EXPORTER_OTLP_ENDPOINT`` in the environment, no OTel provider is
installed and the API's no-op implementations make every span and counter free.
Sentry is the same shape, gated on ``SENTRY_DSN`` instead. Logging is always
configured, because a local run benefits from it too.

Requires the ``observability`` extra; Sentry additionally requires the
``sentry`` extra (kept separate since not every deployment wants it).
"""

from witan_core.observability.asgi import (
    TraceContextASGIMiddleware,
    trace_context_middleware,
)
from witan_core.observability.logging import (
    configure_logging,
    get_logger,
    reset_logging,
)
from witan_core.observability.telemetry import (
    auto_instrument,
    configure_metrics,
    configure_sentry,
    configure_tracing,
    reset_telemetry,
)

__all__ = [
    "TraceContextASGIMiddleware",
    "auto_instrument",
    "configure_logging",
    "configure_metrics",
    "configure_observability",
    "configure_sentry",
    "configure_tracing",
    "get_logger",
    "trace_context_middleware",
    "reset_logging",
    "reset_telemetry",
]


def configure_observability(
    *,
    log_format: str | None = None,
    level: str | None = None,
    instrument: bool = True,
) -> None:
    """Configure logging, tracing, metrics and Sentry in the order they depend on.

    Logging goes first so that a failure while setting up telemetry has somewhere
    to be reported — the alternative is a warning emitted through an unconfigured
    root logger, which is how a broken exporter stays invisible. Sentry goes last
    and independently of the OTel calls before it: it hooks the logging chain
    those calls report through, not the OTel providers, so it has no dependency
    on either succeeding.

    :param log_format: ``console`` or ``json``. Defaults to whether stderr is a
        terminal, so a deployed pod emits JSON and a developer gets colors.
    :param level: Log level name. Defaults to ``WITAN_LOG_LEVEL``/``LOG_LEVEL``.
    :param instrument: Apply installed OTel instrumentors. Turn off to keep a
        third-party instrumentor out of a process that does not want it.
    """
    configure_logging(log_format=log_format, level=level)
    configure_tracing()
    configure_metrics()
    if instrument:
        auto_instrument()
    configure_sentry()
