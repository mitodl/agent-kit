"""Tests for the ASGI trace-context middleware.

These pin the ADOPTION, not that telemetry happens. The defect this fixes was
not "no spans" — witan emitted spans throughout — it was that they started
their own trace instead of the caller's, which looks identical in every metric
and only shows up when you ask a trace which services it contains.
"""

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from witan_core.observability.asgi import (
    TraceContextASGIMiddleware,
    trace_context_middleware,
)

CALLER_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
CALLER_SPAN_ID = "00f067aa0ba902b7"
TRACEPARENT = f"00-{CALLER_TRACE_ID}-{CALLER_SPAN_ID}-01"


@pytest.fixture
def exporter():
    """A tracer provider whose spans we can read back."""
    store = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(store))
    return store, provider


def _scope(headers=None, scope_type="http"):
    return {
        "type": scope_type,
        "headers": [
            (k.encode("latin-1"), v.encode("latin-1"))
            for k, v in (headers or {}).items()
        ],
    }


def _drive(provider, scope, span_name="downstream"):
    """Run the middleware over an app that opens one span, and return it."""
    opened = {}

    async def app(_scope, _receive, _send):
        # Stands in for FastMCP building its `tools/call` span: no explicit
        # parent, so it takes whatever the ambient context is.
        with provider.get_tracer("test").start_as_current_span(span_name) as span:
            opened["span"] = span

    asyncio.run(TraceContextASGIMiddleware(app)(scope, None, None))
    return opened.get("span")


def test_span_opened_downstream_joins_the_callers_trace(exporter):
    store, provider = exporter

    _drive(provider, _scope({"traceparent": TRACEPARENT}))

    (span,) = store.get_finished_spans()
    assert format(span.context.trace_id, "032x") == CALLER_TRACE_ID, (
        "downstream span started its own trace instead of adopting the caller's"
    )
    assert span.parent is not None, "downstream span is a root — nothing was adopted"
    assert format(span.parent.span_id, "016x") == CALLER_SPAN_ID


def test_header_lookup_is_case_insensitive(exporter):
    # HTTP header names are case-insensitive; the W3C propagator looks up an
    # exact lowercase key. A proxy that sends `Traceparent` must still work.
    store, provider = exporter

    _drive(provider, _scope({"TraceParent": TRACEPARENT}))

    (span,) = store.get_finished_spans()
    assert format(span.context.trace_id, "032x") == CALLER_TRACE_ID


def test_no_traceparent_leaves_the_span_a_root(exporter):
    # Direct clients and health checks arrive without one. That must be an
    # ordinary root span, not an error.
    store, provider = exporter

    _drive(provider, _scope({"accept": "application/json"}))

    (span,) = store.get_finished_spans()
    assert span.parent is None


def test_malformed_traceparent_does_not_break_the_request():
    # Telemetry must never fail a request. A junk header is ignored.
    ran = []

    async def app(_scope, _receive, _send):
        ran.append(1)

    asyncio.run(
        TraceContextASGIMiddleware(app)(_scope({"traceparent": "junk"}), None, None)
    )
    assert ran == [1]


def test_non_http_scope_passes_straight_through():
    # lifespan/websocket scopes have no headers to read and must not be touched.
    seen = []

    async def app(scope, _receive, _send):
        seen.append(scope["type"])

    asyncio.run(
        TraceContextASGIMiddleware(app)(_scope(scope_type="lifespan"), None, None)
    )
    assert seen == ["lifespan"]


def test_context_does_not_leak_past_the_request(exporter):
    # ★ The failure this guards is cross-request contamination: a leaked token
    # would parent the NEXT request's spans to the previous caller, which is
    # worse than no propagation because the trace looks plausible.
    store, provider = exporter

    _drive(provider, _scope({"traceparent": TRACEPARENT}))
    store.clear()

    with provider.get_tracer("test").start_as_current_span("after") as span:
        pass

    assert span.parent is None, "trace context outlived the request that carried it"
    assert format(span.context.trace_id, "032x") != CALLER_TRACE_ID


def test_middleware_is_offered_in_the_shape_fastmcp_wants():
    # `run(transport="http", middleware=...)` forwards to `http_app`, which
    # expects Starlette `Middleware` objects. Pinning the shape here means a
    # signature change upstream fails a test rather than a deploy.
    from starlette.middleware import Middleware

    (entry,) = trace_context_middleware()
    assert isinstance(entry, Middleware)
    assert entry.cls is TraceContextASGIMiddleware
