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

from witan_core.observability.logging import get_logger

log = get_logger(__name__)

try:
    from fastmcp.server.middleware import Middleware
except ImportError:  # pragma: no cover - requires the `mcp` extra
    Middleware = object  # type: ignore[assignment,misc]


def _tracer() -> Any:
    """The tracer, or a no-op when OTel is not installed/configured."""
    try:
        from opentelemetry import trace
    except ImportError:  # pragma: no cover - requires the `observability` extra
        return None
    return trace.get_tracer("witan")


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
        # carries the tool it belongs to.
        structlog.contextvars.bind_contextvars(tool=name)
        started = time.perf_counter()
        outcome = "error"
        span_cm = (
            self._tracer.start_as_current_span(f"mcp.tool/{name}")
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
            attributes = {"tool": name, "outcome": outcome}
            if self._counter is not None:
                self._counter.add(1, attributes)
            if self._histogram is not None:
                self._histogram.record(elapsed_ms, attributes)
            log.info(
                "mcp.tool_call", tool=name, outcome=outcome, duration_ms=elapsed_ms
            )
            structlog.contextvars.unbind_contextvars("tool")


class _null_context:  # noqa: N801 - context-manager sentinel, not a public class
    """Stand-in span context for when no tracer is available."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> bool:
        return False
