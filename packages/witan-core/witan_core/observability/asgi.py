"""ASGI middleware that adopts the caller's W3C trace context.

★ WHY THIS EXISTS: TWO HALVES THAT BOTH WORK AND STILL DO NOT MEET ★
ToolHive and FastMCP each implement distributed tracing, against different
carriers, so a request crosses the boundary with its context intact and nothing
picks it up:

* **ToolHive injects HTTP HEADERS.** Both hops do it and only that:
  ``pkg/transport/proxy/transparent/transparent_proxy.go`` --
  ``Inject(ctx, propagation.HeaderCarrier(pr.Out.Header))`` -- and
  ``pkg/vmcp/client/client.go`` ("Inject W3C Trace Context headers
  (traceparent/tracestate) into outgoing requests").
* **FastMCP reads MCP ``_meta``.** ``server/telemetry.py``
  ``_get_parent_trace_context()`` -> ``extract_trace_context(req_ctx.meta)``,
  which it passes as the parent when building its ``tools/call`` span.
* ToolHive HAS a ``_meta`` injector -- ``InjectMetaTraceContext``,
  ``pkg/telemetry/propagation.go`` -- and NOTHING CALLS IT. Searching the repo
  for the symbol returns its own file and its own test.

So the traceparent sits in a header no one in this process reads, ``_meta`` is
empty, FastMCP gets ``None``, and every span here starts a rival root trace.
Measured in QA on 2026-08-14: a tool call produced a 12-span ToolHive trace and
a wholly separate ``qa-witan`` root, with the proxy -> witan boundary therefore
unmeasurable.

★ THIS ATTACHES CONTEXT AND CREATES NO SPAN, ON PURPOSE ★
The obvious alternative is ``opentelemetry-instrumentation-starlette``, which
``auto_instrument`` would pick up with no code at all. It works -- verified --
but it emits a SERVER span plus four ``http receive``/``http send`` children per
request, ~3.5x the span volume, and its ``exclude_spans`` option is NOT honoured
through the global instrumentor (also verified: still 7 spans with it set).

Attaching alone is enough because FastMCP passes ``context=None`` when ``_meta``
is empty, and ``None`` means "use ambient" -- which is what this sets. One span
per call instead of seven, no new dependency, same join. Confirmed end to end
against a live streamable-http server: ``tools/call`` came back parented
directly to the caller's span id.
"""

from __future__ import annotations

from typing import Any


class TraceContextASGIMiddleware:
    """Adopt the ``traceparent``/``tracestate`` of the incoming HTTP request.

    Registered through FastMCP's ``http_app(middleware=...)`` hook, which
    ``run(transport="http", middleware=[...])`` forwards to. It has to be ASGI
    rather than FastMCP middleware: FastMCP builds its span at the protocol
    layer, BEFORE the FastMCP middleware chain runs, so a middleware there
    parents its own span and leaves FastMCP's as the rival root.

    A no-op when OpenTelemetry is absent, matching the rest of this package:
    the ``observability`` extra is optional and a CLI must not require it.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        try:
            from opentelemetry import context as otel_context
            from opentelemetry.propagate import extract
        except ImportError:  # pragma: no cover - requires the `observability` extra
            await self.app(scope, receive, send)
            return

        # latin-1 is what ASGI specifies for raw header bytes, and lowercasing
        # is required because the W3C propagator looks up an exact key while
        # HTTP header names are case-insensitive.
        carrier = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers") or []
        }
        # NOT seeded with the current context: an ASGI scope is the start of a
        # request, so there is no ambient span worth preserving, and seeding
        # would let a previous request's context leak in if the server ever
        # reuses a task. `extract` on a carrier with no traceparent returns an
        # empty context, and attaching that is exactly right here -- it means
        # "this request has no parent".
        token = otel_context.attach(extract(carrier))
        try:
            await self.app(scope, receive, send)
        finally:
            otel_context.detach(token)


def trace_context_middleware() -> list[Any]:
    """This middleware in the shape ``FastMCP.http_app`` wants, or ``[]``.

    Returns a list so a caller can splice it into ``run(..., middleware=...)``
    unconditionally. Empty when Starlette is unavailable, which is the stdio
    case -- there is no HTTP layer to wrap and nothing to adopt.
    """
    try:
        from starlette.middleware import Middleware
    except ImportError:  # pragma: no cover - requires the `mcp` extra
        return []
    return [Middleware(TraceContextASGIMiddleware)]
