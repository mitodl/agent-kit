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
from witan_core.refusal import Refusal

log = get_logger(__name__)

try:
    from fastmcp.server.middleware import Middleware
except ImportError:  # pragma: no cover - requires the `mcp` extra
    Middleware = object  # type: ignore[assignment,misc]

try:
    from fastmcp.server.dependencies import get_access_token
except ImportError:  # pragma: no cover - requires the `mcp` extra
    get_access_token = None  # type: ignore[assignment]


def _message_withholding_types() -> tuple[type, ...]:
    """Exception types whose ``str()`` echoes the caller's own arguments.

    pydantic renders a failed field as ``... [type=string_type,
    input_value=<what the caller sent>, input_type=dict]``, and fastmcp's
    ``ValidationError`` is constructed as ``ValidationError(str(pydantic_exc))``
    (``tools/function_tool.py``), so the caller's value is the message. Both are
    listed because either can arrive depending on which layer validated.
    """
    found: list[type] = []
    for module, name in (
        ("fastmcp.exceptions", "ValidationError"),
        ("pydantic", "ValidationError"),
    ):
        try:
            found.append(getattr(__import__(module, fromlist=[name]), name))
        except (ImportError, AttributeError):  # pragma: no cover - extra absent
            continue
    return tuple(found)


_MESSAGE_WITHHELD = _message_withholding_types()

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


_MAX_ERROR_CHARS = 500
"""How much of a failure's message reaches the ``mcp.tool_call`` line.

Sized against the longest refusal either server raises today: witan-code's
``ClusterGraphMissing``, which renders to 386 characters for a real repo and
endpoint (the sentence, then the provisioning explanation, then the
index-locally hint). Truncation is for a pathological message, not a routine
one, so the bound has to clear that with room rather than sit near it.
"""


def _error_fields(exc: BaseException) -> dict[str, Any]:
    """Why a call failed, as log fields.

    fastmcp logs a ``FastMCPError`` as ``Error calling tool '<name>'`` with
    ``exc_info=False`` and never renders ``str(exc)``, so a refusal reaches the
    log with no text at all. On 2026-09-16 that left Production showing
    ``code_store_views`` erroring on 27% of calls with nothing in the log naming
    a reason; the reason (``ClusterGraphMissing``, a repo with no cluster graph)
    was in the Tempo span's status message and nowhere else. This puts it on the
    line that already records the failure.

    ★ THE MESSAGE IS WITHHELD FOR A VALIDATION ERROR ★
    "fastmcp already sends the client this string" is NOT a reason to log it:
    the client boundary and the Loki boundary are not the same boundary, and
    fastmcp itself draws them differently. Its ``_validation_error_summary``
    exists solely to log counts and codes -- ``error.errors(include_input=False)``
    under the docstring "never input-derived validation details" -- while the
    detail still goes to the caller. A pydantic message embeds
    ``input_value=<what the caller sent>``, so logging it would ship arbitrary
    tool arguments to Loki: the content of a ``memory_store``, a token pasted
    into the wrong field. Nothing is lost by withholding it, because fastmcp
    logs its own input-free summary on that same arm.

    Outside that case the message is safe and adds no exposure. An ordinary
    exception already reaches the log in full through fastmcp's
    ``logger.exception``; a ``Refusal``'s message is written to be read by the
    caller, and the types that could carry something sensitive say so
    explicitly -- ``scan.enforce.WriteBlocked`` carries a field name, a detector
    id and a masked preview, never the matched value.

    ``refused`` separates a call declined on purpose from a service that broke.
    It is LOG-ONLY: ``witan_tool_calls_total`` keeps counting both under
    ``outcome="error"``, because the error-ratio alert's headline case -- a
    quarantined graph answering every request with "not served" -- is itself a
    refusal, and moving refusals to their own outcome would stop that alert
    firing on exactly what it was written for.

    ★ ``error_type`` IS THE REAL CLASS ONLY FOR A ``FastMCPError`` ★
    Anything else is re-raised by ``FastMCP.call_tool`` as
    ``ToolError(f"Error calling tool {name!r}: {e}")`` BEFORE it reaches this
    middleware, so a ``RuntimeError`` out of a tool body logs as ``ToolError``
    with the original message inside ``error``. Refusals keep their own class,
    which is the case this field was added for.

    Never raises: the module's standing rule, the same one
    :func:`_caller_identity` documents. A failure to describe a failure must not
    replace it -- this runs inside an ``except`` block, so an exception here
    would hand the caller the wrong error entirely.
    """
    try:
        fields: dict[str, Any] = {
            "error_type": type(exc).__name__,
            "refused": isinstance(exc, Refusal),
        }
        if _MESSAGE_WITHHELD and isinstance(exc, _MESSAGE_WITHHELD):
            fields["error_withheld"] = True
            return fields
        message = str(exc).strip()
        if len(message) > _MAX_ERROR_CHARS:
            message = message[:_MAX_ERROR_CHARS] + "..."
        if message:
            fields["error"] = message
    except Exception:  # noqa: BLE001 - see docstring; never fail the call
        return {"error_type": "unknown"}
    return fields


def _is_input_required(result: Any) -> bool:
    """Whether MRTR converted this call into an elicitation.

    Checked by class name rather than isinstance so this module does not have to
    import a fastmcp internal that moved once already between 3.4.x and 4.x.
    """
    return type(result).__name__ == "InputRequiredToolResult"


class ObservabilityMiddleware(Middleware):  # type: ignore[misc,valid-type]
    """Emit a span, a counter increment and a duration sample per tool call.

    A failed call also carries why it failed: ``error_type``, ``error`` and
    ``refused``, since fastmcp renders neither the message nor the class on its
    own error line. Two limits are deliberate and documented in
    :func:`_error_fields`: an argument-validation failure withholds its message
    (it embeds the caller's own input), and ``error_type`` is the real class
    only for a ``FastMCPError``, which in practice means the refusals this was
    added for.
    """

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
        error_fields: dict[str, Any] = {}
        span_cm = (
            self._tracer.start_as_current_span(
                f"mcp.tool/{name}",
                # No explicit parent: ambient is correct here. The caller's
                # context is adopted at the ASGI layer
                # (`witan_core.observability.asgi`) before FastMCP builds its
                # own span, so this one nests under both without asking.
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
        except BaseException as exc:
            # BaseException, not Exception: a cancelled call is a real outcome
            # worth naming, and re-raising keeps cancellation semantics intact.
            error_fields = _error_fields(exc)
            raise
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
                **error_fields,
            )
            structlog.contextvars.unbind_contextvars("tool", *identity)


class _null_context:  # noqa: N801 - context-manager sentinel, not a public class
    """Stand-in span context for when no tracer is available."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> bool:
        return False
