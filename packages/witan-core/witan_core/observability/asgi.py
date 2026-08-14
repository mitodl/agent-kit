"""ASGI middleware that adopts the caller's W3C trace context.

★ WHY THIS EXISTS: TWO HALVES THAT BOTH WORK AND STILL DO NOT MEET ★
ToolHive and FastMCP each implement distributed tracing, against different
carriers, so a request crosses the boundary with its context intact and nothing
picks it up:

* **The hop that reaches this process injects HTTP HEADERS.**
  ``pkg/transport/proxy/transparent/transparent_proxy.go`` --
  ``Inject(ctx, propagation.HeaderCarrier(pr.Out.Header))``. The vMCP's own
  client does the same on its outbound requests
  (``pkg/vmcp/client/client.go``).
* **FastMCP reads MCP ``_meta``.** ``server/telemetry.py``
  ``_get_parent_trace_context()`` -> ``extract_trace_context(req_ctx.meta)``,
  which it passes as the parent when building its ``tools/call`` span.

ToolHive does ALSO inject ``_meta`` in one place --
``pkg/vmcp/session/internal/backend/mcp_session.go`` wraps outgoing
``CallTool`` params in ``telemetry.MetaWithTraceContext``. So this is not a
missing feature upstream; it is that whatever path our traffic takes does not
deliver a usable ``_meta`` to the backend, and the header is what actually
arrives.

★ THE EMPIRICAL FACT, WHICH IS WHAT THIS CODE RESTS ON: measured against QA on
2026-08-14 with witan-core 0.19.0, a tool call produced a ToolHive trace and a
wholly separate ``qa-witan`` root (``serviceStats {qa-witan: 3}``, ToolHive
absent), so FastMCP got no parent from ``_meta``. Adopting the header — which
the proxy demonstrably sets — closes it. Do not rewrite this comment into a
claim about upstream's intent without re-measuring; an earlier version of it
asserted ToolHive had no ``_meta`` injector at all, which is false.

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
