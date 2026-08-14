"""FastMCP middleware that traces and counts every tool call.

★ REGISTER THIS BEFORE ``MRTRElicitationMiddleware`` ★
FastMCP composes its chain with ``for mw in reversed(self.middleware)``, so the
first middleware added ends up outermost. That ordering is load-bearing here:
``MRTRElicitationMiddleware`` catches a raised ``InputRequired`` and converts it
into a *successful* ``InputRequiredToolResult``. Registered outside it, this
middleware sees that success and can label it ``input_required``; registered
inside it, it would instead see a raised exception and record every ordinary
"the server asked the user a question" as a failed call, inflating the error
rate with normal interactions.

★ THIS IS THE ONLY TIER THAT CAN SAY *WHO* ★
witan holds the validated JWT; omnigraph-server downstream only ever sees the
bearer token filed under an actor id, so its policy log can name ``act-<sub>``
and nothing else. Every ``mcp.tool_call`` line therefore carries ``actor_id`` —
a sentinel when there is no usable claim, never a blank — which is what makes
two users' concurrent traffic separable in Loki rather than one undifferentiated
stream. The human-readable ``actor`` rides alongside it whenever the token names
somebody, and is omitted rather than invented when it does not (local stdio, or
a token with no username/email claim). See :func:`_caller_identity`.

★ CLI CALLS DO NOT PASS THROUGH HERE ★
``witan/cli/_common.py:_fn`` unwraps ``@mcp.tool`` functions and calls the plain
Python function, bypassing the middleware chain entirely. So these metrics count
MCP traffic — agents and the remote CLI — and not local in-process CLI use. That
is the right denominator for a *shared service*, but it means "witan tool calls"
here is not the same population as "times anyone ran a witan command".
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from witan_core.identity import derive_actor_handle, derive_actor_id
from witan_core.observability.logging import get_logger

log = get_logger(__name__)

try:
    from fastmcp.server.middleware import Middleware
except ImportError:  # pragma: no cover - requires the `mcp` extra
    Middleware = object  # type: ignore[assignment,misc]

try:
    from fastmcp.server.dependencies import get_access_token
except ImportError:  # pragma: no cover - requires the `mcp` extra
    get_access_token = None  # type: ignore[assignment]

LOCAL_ACTOR = "local"
"""``actor_id`` for a call that carried no JWT.

Deployed, this value should never appear: FastMCP's verifier rejects an
unauthenticated request before any tool handler runs, so a tool call that
reaches this middleware always has a token. It therefore means the server is
running without OIDC configured — local stdio/http use, where there is one
user and ``cfg.author`` is already the right attribution.

The *other* identity-less case ADR-0004 names — an admin/migration command run
inside the deployed container — cannot reach here at all: ``cli/_common.py:_fn``
unwraps ``@mcp.tool`` functions and calls the plain Python function, bypassing
the middleware chain (see this module's header).
"""

UNKNOWN_ACTOR = "unknown"
"""``actor_id`` for a token whose ``sub`` will not derive one.

Distinct from :data:`LOCAL_ACTOR` on purpose: "nobody was authenticated" and
"somebody was, but their claim is unusable" want different responses, and
collapsing them would hide a malformed-token bug behind normal local use.
"""


def _tracer() -> Any:
    """The tracer, or a no-op when OTel is not installed/configured."""
    try:
        from opentelemetry import trace
    except ImportError:  # pragma: no cover - requires the `observability` extra
        return None
    return trace.get_tracer("witan")


def _parent_context(context: Any) -> Any:
    """The caller's trace context, pulled out of the request's MCP ``_meta``.

    ★ ToolHive PROPAGATES THROUGH ``_meta``, NOT AN HTTP HEADER. Its
    ``InjectMetaTraceContext`` (``pkg/telemetry/propagation.go``) writes
    ``traceparent``/``tracestate`` into the JSON-RPC ``_meta`` object, because
    that is the only channel that survives an MCP hop. Nothing in this process
    reads HTTP headers — there is no ASGI instrumentation — so without this
    function every span here is a parentless ROOT.

    That was the state until 2026-08-14, and it cost two things at once:

    1. witan's spans never joined ToolHive's trace, so the proxy -> witan
       boundary could not be separated per request. Measured in QA: one
       ``memory_search`` produced a 12-span trace across ``*-vmcp`` and
       ``*-mcp-proxy``, with witan in none of it.
    2. ``OTEL_TRACES_SAMPLER=parentbased_traceidratio`` silently degraded to
       its ROOT sampler. With no parent to inherit from it re-rolled the ratio
       locally, so witan sampled 0.25 where ToolHive had already decided 1.0 —
       which is why zero of six QA calls produced a trace at all.

    Returns ``None`` when there is no usable context, which
    ``start_as_current_span`` treats as "use the ambient one" — the correct
    behaviour for stdio and local CLI use, where no ``_meta`` exists.
    """
    try:
        from opentelemetry.propagate import extract
    except ImportError:  # pragma: no cover - requires the `observability` extra
        return None
    meta = getattr(getattr(context, "message", None), "meta", None)
    if not meta:
        return None
    # `RequestParamsMeta` is a TypedDict, so this is already a plain mapping —
    # copied rather than passed through because the propagator's getter is
    # documented against a Mapping and this keeps a model's view from leaking.
    return extract(dict(meta))


def _instruments() -> tuple[Any, Any]:
    """The call counter and duration histogram, or ``(None, None)``."""
    try:
        from opentelemetry import metrics
    except ImportError:  # pragma: no cover - requires the `observability` extra
        return None, None
    meter = metrics.get_meter("witan")
    return (
        meter.create_counter(
            "witan.tool.calls", description="MCP tool invocations", unit="1"
        ),
        meter.create_histogram(
            "witan.tool.duration", description="MCP tool call duration", unit="ms"
        ),
    )


def _caller_identity() -> dict[str, str]:
    """Who is making the current tool call, as log fields.

    Always returns an ``actor_id`` — a sentinel rather than a blank or a
    missing key — so "who did this" is answerable from any ``mcp.tool_call``
    line instead of only from the ones that happened to be authenticated.
    ``actor`` (the human handle) is omitted when there is nobody to name,
    since inventing one would be worse than its absence.

    Never raises: an observability layer that can fail a tool call is a worse
    problem than a log line missing its identity.
    """
    if get_access_token is None:  # pragma: no cover - requires the `mcp` extra
        return {"actor_id": LOCAL_ACTOR}
    try:
        token = get_access_token()
    except Exception:  # noqa: BLE001 - see docstring; never fail the call
        return {"actor_id": UNKNOWN_ACTOR}
    if token is None:
        return {"actor_id": LOCAL_ACTOR}
    claims = getattr(token, "claims", None) or {}
    try:
        fields = {"actor_id": derive_actor_id(claims.get("sub", ""))}
    except ValueError:
        fields = {"actor_id": UNKNOWN_ACTOR}
    handle = derive_actor_handle(claims)
    if handle:
        fields["actor"] = handle
    return fields


def _is_input_required(result: Any) -> bool:
    """Whether MRTR converted this call into an elicitation.

    Checked by class name rather than isinstance so this module does not have to
    import a fastmcp internal that moved once already between 3.4.x and 4.x.
    """
    return type(result).__name__ == "InputRequiredToolResult"


class ObservabilityMiddleware(Middleware):  # type: ignore[misc,valid-type]
    """Emit a span, a counter increment and a duration sample per tool call."""

    def __init__(self) -> None:
        self._tracer = _tracer()
        self._counter, self._histogram = _instruments()

    async def on_call_tool(self, context, call_next):  # noqa: ANN001, ANN201
        name = getattr(getattr(context, "message", None), "name", None) or "unknown"
        # Bound here rather than passed down, so every log line emitted anywhere
        # beneath this call — including inside a dependency's stdlib logger —
        # carries the tool it belongs to, and who called it. Identity is
        # resolved once per call: `get_access_token()` reads a per-request
        # contextvar, so it is only in scope on this side of `call_next`.
        identity = _caller_identity()
        structlog.contextvars.bind_contextvars(tool=name, **identity)
        started = time.perf_counter()
        outcome = "error"
        span_cm = (
            self._tracer.start_as_current_span(
                f"mcp.tool/{name}",
                # The caller's context, so this span nests under ToolHive's
                # rather than starting a rival root trace. See
                # `_parent_context` for why this cannot come from a header.
                context=_parent_context(context),
                # The caller's context, so this span nests under ToolHive's
                # rather than starting a rival root trace. See
                # `_parent_context` for why this cannot come from a header.
                # The caller's context, so this span nests under ToolHive's
                # rather than starting a rival root trace. See
                # `_parent_context` for why this cannot come from a header.
                attributes=dict(identity),
            )
            if self._tracer
            else _null_context()
        )
        try:
            with span_cm:
                result = await call_next(context)
                outcome = "input_required" if _is_input_required(result) else "ok"
                return result
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            # Identity is deliberately NOT a metric attribute. Every label here
            # multiplies the stored series, and per-actor tool/outcome counters
            # would fan out with the user count for a question ("who calls
            # what, how often") the logs and spans already answer.
            attributes = {"tool": name, "outcome": outcome}
            if self._counter is not None:
                self._counter.add(1, attributes)
            if self._histogram is not None:
                self._histogram.record(elapsed_ms, attributes)
            log.info(
                "mcp.tool_call",
                tool=name,
                outcome=outcome,
                duration_ms=elapsed_ms,
                **identity,
            )
            structlog.contextvars.unbind_contextvars("tool", *identity)


class _null_context:  # noqa: N801 - context-manager sentinel, not a public class
    """Stand-in span context for when no tracer is available."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> bool:
        return False
