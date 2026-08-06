"""Tests for witan_core.observability."""

import json
import logging

import pytest
import structlog

from witan_core.observability import configure_observability
from witan_core.observability.logging import (
    configure_logging,
    get_logger,
    reset_logging,
)
from witan_core.observability.processors import inject_k8s_context, inject_otel_context
from witan_core.observability.telemetry import (
    configure_metrics,
    configure_tracing,
    reset_telemetry,
)


@pytest.fixture(autouse=True)
def _clean_config():
    """Every test starts from an unconfigured process and leaves one behind."""
    reset_logging()
    reset_telemetry()
    yield
    reset_logging()
    reset_telemetry()
    # dictConfig installed a handler on the root logger; drop it so a later
    # test's capsys/caplog sees a clean slate.
    logging.getLogger().handlers.clear()


def _emit_and_capture(capsys, **kwargs):
    configure_logging(log_format="json", level="INFO")
    get_logger("test").info("hello", **kwargs)
    return capsys.readouterr()


# ── the constraint that breaks stdio if it regresses ──────────────────────────
def test_all_output_goes_to_stderr(capsys):
    # `witan serve --transport stdio` speaks MCP over stdout. A log line there
    # corrupts the framing and kills the session. logging.StreamHandler happens
    # to default to stderr, so this asserts the behaviour rather than trusting
    # the default to survive an edit.
    captured = _emit_and_capture(capsys)
    assert captured.out == ""
    assert "hello" in captured.err


def test_stdlib_logging_also_goes_to_stderr(capsys):
    # Foreign records travel the same handler, so a dependency's logger cannot
    # corrupt stdio either.
    configure_logging(log_format="json", level="INFO")
    logging.getLogger("some.dependency").warning("from stdlib")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "from stdlib" in captured.err


# ── rendering ─────────────────────────────────────────────────────────────────
def test_json_format_renders_parseable_json(capsys):
    captured = _emit_and_capture(capsys, actor="svc-witan-ci")
    payload = json.loads(captured.err.strip())
    assert payload["event"] == "hello"
    assert payload["actor"] == "svc-witan-ci"
    assert payload["level"] == "info"
    assert payload["logger"] == "test"
    assert "timestamp" in payload


def test_stdlib_records_render_in_the_same_shape(capsys):
    # The point of the ProcessorFormatter bridge: a dependency's plain logger
    # comes out as the same JSON object our own calls produce.
    configure_logging(log_format="json", level="INFO")
    logging.getLogger("dep").warning("legacy %s", "message")
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["event"] == "legacy message"
    assert payload["level"] == "warning"


def test_console_format_is_not_json(capsys):
    configure_logging(log_format="console", level="INFO")
    get_logger("test").info("hello")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hello" in captured.err
    with pytest.raises(json.JSONDecodeError):
        json.loads(captured.err.strip())


def test_exception_is_rendered_for_structlog_events(capsys):
    configure_logging(log_format="json", level="INFO")
    try:
        msg = "boom"
        raise ValueError(msg)
    except ValueError:
        get_logger("test").exception("failed")
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["exception"]


def test_exception_is_rendered_for_stdlib_records(capsys):
    # The reason _EXCEPTION_RENDERER appears in both the pipeline and the
    # formatter: a foreign record's exc_info is attached after the pipeline has
    # run, so only the formatter copy can render it. Dropping that copy loses
    # every traceback logged by a dependency.
    configure_logging(log_format="json", level="INFO")
    try:
        msg = "boom"
        raise ValueError(msg)
    except ValueError:
        logging.getLogger("dep").exception("failed")
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["exception"]


def test_level_filters_below_threshold(capsys):
    configure_logging(log_format="json", level="WARNING")
    get_logger("test").info("suppressed")
    get_logger("test").warning("kept")
    err = capsys.readouterr().err
    assert "suppressed" not in err
    assert "kept" in err


def test_contextvars_are_merged(capsys):
    # How per-tool-call actor binding reaches every downstream log line without
    # threading a logger through the call stack.
    configure_logging(log_format="json", level="INFO")
    structlog.contextvars.bind_contextvars(actor_id="act-123")
    try:
        get_logger("test").info("hello")
    finally:
        structlog.contextvars.clear_contextvars()
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["actor_id"] == "act-123"


# ── idempotency ───────────────────────────────────────────────────────────────
def test_configure_is_idempotent(capsys):
    # Both `witan serve` and `witan-code serve` call this, and mounting one
    # server into the other puts both in one process. A second call must not
    # add a second handler and double every line.
    configure_logging(log_format="json", level="INFO")
    configure_logging(log_format="json", level="INFO")
    get_logger("test").info("once")
    assert capsys.readouterr().err.strip().count("once") == 1


def test_force_reconfigures():
    configure_logging(log_format="json", level="INFO")
    configure_logging(log_format="console", level="DEBUG", force=True)
    assert logging.getLogger().level == logging.DEBUG


# ── processors ────────────────────────────────────────────────────────────────
def test_otel_processor_is_a_noop_without_a_span():
    # No provider installed -> the API's no-op span -> nothing to correlate.
    # Emitting ids here would point at traces that were never exported.
    event = inject_otel_context(None, "info", {"event": "x"})
    assert "trace_id" not in event
    assert "span_id" not in event


def test_otel_processor_respects_preexisting_ids():
    event = inject_otel_context(None, "info", {"trace_id": "a", "span_id": "b"})
    assert event["trace_id"] == "a"


def test_k8s_processor_is_a_noop_outside_a_cluster(monkeypatch):
    monkeypatch.delenv("KUBERNETES_POD_NAME", raising=False)
    import importlib

    from witan_core.observability import processors

    importlib.reload(processors)
    assert processors.inject_k8s_context(None, "info", {"event": "x"}) == {"event": "x"}


def test_k8s_processor_adds_pod_context(monkeypatch):
    # Read once at import, so the module has to be reloaded to observe the env.
    monkeypatch.setenv("KUBERNETES_POD_NAME", "witan-0")
    monkeypatch.setenv("KUBERNETES_NAMESPACE", "witan")
    import importlib

    from witan_core.observability import processors

    importlib.reload(processors)
    event = processors.inject_k8s_context(None, "info", {"event": "x"})
    assert event["pod_name"] == "witan-0"
    assert event["namespace"] == "witan"
    importlib.reload(processors)


def test_k8s_processor_import_is_stable():
    assert inject_k8s_context(None, "info", {"event": "x"})["event"] == "x"


# ── telemetry gating ──────────────────────────────────────────────────────────
def test_tracing_is_skipped_without_an_endpoint(monkeypatch):
    # Every local CLI run and every stdio session lands here. No provider is
    # installed, so the API's no-op tracer makes instrumentation free and it can
    # be written unconditionally at the call sites.
    for var in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    ):
        monkeypatch.delenv(var, raising=False)
    assert configure_tracing() is None
    assert configure_metrics() is None


def _shutdown(provider):
    """Stop a provider's background exporter.

    These tests install real OTLP exporters pointed at a port nothing is
    listening on. Left running, their batch processors keep retrying on daemon
    threads for the rest of the session and spray connection errors over
    unrelated tests' output. Shutting down here keeps the failure contained to
    the test that asked for it.
    """
    provider.shutdown()


@pytest.fixture
def _fast_exporter(monkeypatch):
    """Fail the doomed export quickly instead of retrying for seconds."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "1")
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "600000")


def test_tracing_is_configured_with_an_endpoint(monkeypatch, _fast_exporter):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    provider = configure_tracing()
    assert provider is not None
    _shutdown(provider)


def test_metrics_are_configured_with_an_endpoint(monkeypatch, _fast_exporter):
    # Beyond the ol-django reference, which sets up tracing only.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    provider = configure_metrics()
    assert provider is not None
    _shutdown(provider)


def test_repeat_calls_return_the_same_provider(monkeypatch, _fast_exporter):
    # The return value answers "what is installed", not "did this call install
    # it". A guard that returned None on the second call would make "already
    # configured" and "not configured" indistinguishable to the caller.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    first = configure_tracing()
    assert configure_tracing() is first
    first_meter = configure_metrics()
    assert configure_metrics() is first_meter
    _shutdown(first)
    _shutdown(first_meter)


def test_signal_specific_endpoint_is_honored(monkeypatch, _fast_exporter):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://localhost:4318/v1/traces"
    )
    provider = configure_tracing()
    assert provider is not None
    _shutdown(provider)


def test_telemetry_setup_never_raises(monkeypatch):
    # A broken exporter must degrade to an un-instrumented process, not take the
    # server down on boot.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    configure_logging(log_format="json", level="INFO")
    # grpc exporter is not installed by the `observability` extra; the import
    # fails inside the try and is swallowed.
    assert configure_tracing() is None


def test_configure_observability_wires_everything(monkeypatch, capsys):
    for var in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        monkeypatch.delenv(var, raising=False)
    configure_observability(log_format="json", level="INFO", instrument=False)
    get_logger("test").info("wired")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err.strip())["event"] == "wired"
