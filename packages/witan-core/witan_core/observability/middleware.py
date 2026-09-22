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


def _optional_type(module: str, name: str) -> type | None:
    """A class that may not be installed, or ``None``.

    Resolved once at import rather than per call. Missing is not an error
    condition here: without fastmcp ``Middleware`` is ``object`` and
    ``on_call_tool`` is never invoked at all, so a ``None`` can only be seen by
    a caller driving this module directly.
    """
    try:
        return getattr(__import__(module, fromlist=[name]), name)
    except (ImportError, AttributeError):  # pragma: no cover - extra absent
        return None


_FASTMCP_VALIDATION_ERROR = _optional_type("fastmcp.exceptions", "ValidationError")
"""fastmcp's wrapper for a call whose ARGUMENTS failed validation.

Built as ``ValidationError(str(pydantic_exc))`` in ``tools/function_tool.py``,
so its message is pydantic's, caller input and all.
"""


def _builtin_validation_codes() -> frozenset[str] | None:
    """pydantic's own ``ErrorType`` literals, or ``None`` if unavailable.

    The allowlist behind :func:`_validation_summary`'s ``error_types``. A
    validator is free to raise ``PydanticCustomError`` with a ``type`` it
    builds at runtime, which can be built out of the value it just rejected --
    so a code that is not one of these is not safe to log.
    """
    try:
        from typing import get_args

        from pydantic_core import ErrorType
    except ImportError:  # pragma: no cover - extra absent
        return None
    return frozenset(get_args(ErrorType))


_BUILTIN_VALIDATION_CODES = _builtin_validation_codes()

_CUSTOM_VALIDATION_CODE = "custom_error"
"""Stands in for any validation code outside :data:`_BUILTIN_VALIDATION_CODES`.

The same substitution fastmcp makes in ``_validation_error_summary``, for the
same reason: the set of built-in codes is fixed and public, and anything else
was assembled by a validator that may have had the caller's value in hand.
"""

_PYDANTIC_VALIDATION_ERROR = _optional_type("pydantic", "ValidationError")
"""pydantic's own error, which reaches the middleware unwrapped.

fastmcp re-raises this one as-is (``server.py``'s ``except
PydanticValidationError`` arm), and it means a model failed validation inside a
TOOL BODY rather than at the call boundary. The two are not related by
inheritance -- fastmcp's is a ``FastMCPError`` -- so both have to be named.
"""


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

Sized for the messages that actually reach this field. The opted-in refusals
are bounded prose over identifiers: the longest, witan-code's
``ClusterGraphMissing``, is a sentence plus a provisioning explanation plus a
hint. What is NOT bounded is an ordinary exception's message, which a tool body
composes however it likes, so the cap exists for that case rather than for the
refusals.

(An earlier version of this paragraph reached for ``ClusterUnreachable`` as the
unbounded example. It is not a ``Refusal`` and never was, so its message has
never reached this field at all.)
"""


def _validation_summary(exc: BaseException) -> dict[str, Any] | None:
    """An input-free description of a validation failure, or ``None``.

    ``None`` means "not a validation error". Both arms land here -- fastmcp's
    ``ValidationError`` for bad ARGUMENTS and a bare ``pydantic.ValidationError``
    for a model failing inside a tool BODY -- and neither gets its message.

    WHY NOT THE MESSAGE, INCLUDING FOR A BODY ERROR. Three routes put somebody's
    data in it, all confirmed by running them:

    - ``include_input=False`` drops the structured ``input`` entry but not
      ``msg``, and for the BUILTIN codes ``value_error`` and
      ``assertion_error`` pydantic renders the raised exception's own text --
      ``"Value error, rejected <the value>"`` for a validator that did
      ``raise ValueError(f"rejected {value}")``. Allowlisting the code does not
      help, because those codes ARE on the allowlist.
    - A ``PydanticCustomError``'s ``type`` and ``msg`` are both written by the
      validator and can be built from the value it just rejected.
    - ``loc`` names the offending key for ``extra_forbidden`` and for a
      dict-key failure, which is the caller's key.

    And the "a body error is always one of our own models" reasoning that an
    earlier revision leaned on was simply wrong: ``witan_code.config._Target``
    is a ``BaseModel`` built straight from TOML by ``_parse_targets``, with no
    ``except ValidationError`` anywhere in that module, and ``cfg_module.load()``
    runs on tool paths (``ingest._config``). So this arm does see externally
    sourced validation.

    What survives is the count and the codes, allowlisted against pydantic's own
    ``ErrorType`` literals -- the same trade fastmcp makes in
    ``_validation_error_summary``. Computed here rather than left to fastmcp's
    version because the ``fastmcp`` logger does not propagate and keeps its own
    handler, so its summary is unparsed text beside our JSON rather than a
    queryable field.
    """
    pydantic_exc: BaseException | None = None
    if _PYDANTIC_VALIDATION_ERROR is not None and isinstance(
        exc, _PYDANTIC_VALIDATION_ERROR
    ):
        pydantic_exc = exc
    elif _FASTMCP_VALIDATION_ERROR is not None and isinstance(
        exc, _FASTMCP_VALIDATION_ERROR
    ):
        # fastmcp raises its wrapper `from` the pydantic error, so the
        # structured detail is one link away even though the message is a
        # flattened string by then.
        cause = exc.__cause__
        if _PYDANTIC_VALIDATION_ERROR is not None and isinstance(
            cause, _PYDANTIC_VALIDATION_ERROR
        ):
            pydantic_exc = cause
    else:
        return None
    fields: dict[str, Any] = {"error_withheld": True}
    if pydantic_exc is None:
        return fields
    details = pydantic_exc.errors(  # type: ignore[attr-defined]
        include_url=False, include_context=False, include_input=False
    )
    fields["error_count"] = len(details)
    if _BUILTIN_VALIDATION_CODES is not None:
        fields["error_types"] = sorted(
            {
                code
                if (code := str(detail.get("type", ""))) in _BUILTIN_VALIDATION_CODES
                else _CUSTOM_VALIDATION_CODE
                for detail in details
            }
        )
    return fields


def _message_fields(exc: BaseException) -> dict[str, Any]:
    """The ``error`` field for a non-validation failure, or the reason there is none.

    ★ A REFUSAL MUST OPT IN ★
    ``Refusal.log_safe_message`` is ``False`` by default and each type sets it
    only after its message has been read. The assumption this replaces -- that a
    refusal's message is safe because it is written for the caller -- does not
    hold: ``witan_code.ingest.IngestRefused`` is raised from ``mutate_many`` as
    ``f"Every step needs a non-empty {field!r}; got {value!r}."`` over the
    caller's own step, and fastmcp's ``FastMCPError`` arm never rendered it, so
    logging it here would be new exposure rather than a restatement.

    Anything that is NOT a refusal keeps its message, and the justification is
    narrower than it looks. A PLAIN exception has already been written to the
    log in full, with a traceback, by fastmcp's ``except Exception`` arm calling
    ``logger.exception`` -- verified by driving a tool that interpolates its
    argument and finding the value in fastmcp's own stderr with this middleware
    absent. So for those the field restates what the pod log already holds.

    That is NOT true of a ``FastMCPError`` which is not a ``Refusal``: it takes
    the ``except FastMCPError`` arm (``exc_info=False``, no ``str(exc)``), so
    its message would be new exposure exactly as a refusal's is. Today the only
    one reaching here is fastmcp's own ``NotFoundError("Unknown tool: ...")``,
    whose payload is the tool name that is already the ``tool`` field, and
    neither server raises a bare ``ToolError`` from a tool body. If that
    changes, this arm needs the same opt-in the refusals have.
    """
    if isinstance(exc, Refusal) and not exc.log_safe_message:
        return {"error_withheld": True}
    return {"error": str(exc)}


def _error_fields(exc: BaseException) -> dict[str, Any]:
    """Why a call failed, as log fields.

    fastmcp logs a ``FastMCPError`` as ``Error calling tool '<name>'`` with
    ``exc_info=False`` and never renders ``str(exc)``, so a refusal reached the
    log with no text at all. On 2026-09-16 that left Production showing
    ``code_store_views`` erroring on 27% of calls with nothing in the log naming
    a reason; the reason (``ClusterGraphMissing``, a repo with no cluster graph)
    was in the Tempo span's status message and nowhere else. ``error_type``
    alone answers that, and it is always present.

    The message is the part that needs a contract, and there are two:
    :func:`_validation_summary` withholds it for every validation failure, and
    :func:`_message_fields` requires a refusal to opt in. A failure that is
    neither keeps its message, because fastmcp has already logged it in full.

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

    Never raises, and means it: this runs inside an ``except`` block, so an
    exception escaping here would REPLACE the caller's failure with a server
    fault -- a clean refusal would surface as a crash. ``BaseException`` rather
    than ``Exception`` because that is what the caller catches, and a partially
    built result is kept rather than discarded, so a refusal that trips the
    guard is still marked ``refused``. The failure is reported as
    ``error_undescribable`` rather than ``error_withheld``, which means the
    other thing: withholding is a decision, this is a breakage.

    That breadth swallows a ``KeyboardInterrupt`` or ``SystemExit`` raised in
    the few bytecodes this spans, and that is the accepted trade rather than an
    oversight. The two cases are indistinguishable from here -- a real signal,
    or a ``__str__`` that raises one -- and of the two, an exception out of
    ``__str__`` replacing a clean refusal is the one that has a mechanism. A
    swallowed interrupt costs a second Ctrl-C; the original failure still
    propagates either way.
    """
    fields: dict[str, Any] = {"error_type": "unknown"}
    try:
        fields["error_type"] = type(exc).__name__
        fields["refused"] = isinstance(exc, Refusal)
        validation = _validation_summary(exc)
        fields.update(validation if validation is not None else _message_fields(exc))
        message = fields.get("error", "").strip()
        if len(message) > _MAX_ERROR_CHARS:
            message = message[:_MAX_ERROR_CHARS] + "..."
        if message:
            fields["error"] = message
        else:
            fields.pop("error", None)
    except BaseException:  # noqa: BLE001 - see docstring; never fail the call
        fields["error_undescribable"] = True
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
