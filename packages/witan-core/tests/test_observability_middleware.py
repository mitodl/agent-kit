"""Tests for the tool-call observability middleware."""

import asyncio
import json

import pytest
import structlog

from witan_core.observability import middleware as middleware_module
from witan_core.observability.logging import configure_logging, reset_logging
from witan_core.observability.middleware import (
    LOCAL_ACTOR,
    UNKNOWN_ACTOR,
    ObservabilityMiddleware,
)
from witan_core.observability.telemetry import reset_telemetry


class _Message:
    def __init__(self, name):
        self.name = name


class _Context:
    def __init__(self, name):
        self.message = _Message(name)


class InputRequiredToolResult:
    """Stands in for fastmcp's class, which the middleware matches by name.

    The name has to match exactly — that is the whole matching rule. See
    ``test_fastmcp_still_names_the_class_this_way``, which fails if upstream
    renames it and this stub silently stops resembling the real thing.
    """


@pytest.fixture(autouse=True)
def _clean():
    reset_logging()
    reset_telemetry()
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()
    reset_logging()
    reset_telemetry()
    import logging

    logging.getLogger().handlers.clear()


def _run(middleware, context, call_next):
    """Configure logging, then drive the middleware coroutine.

    Logging is configured here rather than in the fixture because the handler
    binds to whatever ``sys.stderr`` is at configure time, and capsys swaps
    ``sys.stderr`` for its own object. Configuring from inside the test body
    guarantees the handler points at the one capsys will read back.

    asyncio.run rather than pytest-asyncio: this package has no async test
    plugin and one middleware is not reason enough to add a dependency to it.
    """
    configure_logging(log_format="json", level="INFO", force=True)
    return asyncio.run(middleware.on_call_tool(context, call_next))


def test_successful_call_is_recorded_as_ok(capsys):
    async def call_next(_ctx):
        return "result"

    result = _run(ObservabilityMiddleware(), _Context("task_get"), call_next)
    assert result == "result"
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["event"] == "mcp.tool_call"
    assert payload["tool"] == "task_get"
    assert payload["outcome"] == "ok"
    assert payload["duration_ms"] >= 0


def test_elicitation_is_not_counted_as_an_error(capsys):
    # The reason this middleware must be registered OUTSIDE MRTR. MRTR converts
    # a raised InputRequired into this successful result; recording it as an
    # error would make every ordinary "ask the user" inflate the error rate.
    async def call_next(_ctx):
        return InputRequiredToolResult()

    _run(ObservabilityMiddleware(), _Context("memory_delete"), call_next)
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["outcome"] == "input_required"


def test_failure_is_recorded_and_reraised(capsys):
    async def call_next(_ctx):
        msg = "boom"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom"):
        _run(ObservabilityMiddleware(), _Context("task_close"), call_next)
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["outcome"] == "error"
    assert payload["tool"] == "task_close"


def test_failure_says_what_went_wrong(capsys):
    # The defect this closes: fastmcp logs `Error calling tool 'x'` with
    # exc_info=False for a FastMCPError and never renders str(exc), so
    # Production showed code_store_views failing 27% of calls with no
    # diagnosable text anywhere but the Tempo span.
    async def call_next(_ctx):
        msg = "boom"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom"):
        _run(ObservabilityMiddleware(), _Context("task_close"), call_next)
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["error"] == "boom"
    assert payload["error_type"] == "ValueError"
    assert payload["refused"] is False


def test_a_refusal_is_marked_as_one(capsys):
    # ClusterGraphMissing is the real production case: a repo that has no
    # cluster graph. It is a Refusal, so the line says the service declined
    # rather than broke -- while still counting under outcome="error", which
    # the error-ratio alert depends on.
    from witan_core.refusal import Refusal

    class ClusterGraphMissing(RuntimeError, Refusal):
        pass

    async def call_next(_ctx):
        msg = "'x' code graph is not served by the omnigraph-server"
        raise ClusterGraphMissing(msg)

    with pytest.raises(ClusterGraphMissing):
        _run(ObservabilityMiddleware(), _Context("code_store_views"), call_next)
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["outcome"] == "error"
    assert payload["refused"] is True
    assert payload["error_type"] == "ClusterGraphMissing"
    assert "is not served by the omnigraph-server" in payload["error"]


def test_a_long_message_is_truncated(capsys):
    async def call_next(_ctx):
        raise ValueError("x" * (middleware_module._MAX_ERROR_CHARS + 50))

    with pytest.raises(ValueError):
        _run(ObservabilityMiddleware(), _Context("recall"), call_next)
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["error"] == "x" * middleware_module._MAX_ERROR_CHARS + "..."


def test_a_message_less_failure_still_names_its_type(capsys):
    # An empty `error` key would be worse than none: it reads as "no message
    # was produced" rather than "this exception carries none".
    async def call_next(_ctx):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _run(ObservabilityMiddleware(), _Context("recall"), call_next)
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["error_type"] == "KeyboardInterrupt"
    assert "error" not in payload


def test_a_successful_call_carries_no_error_fields(capsys):
    async def call_next(_ctx):
        return "result"

    _run(ObservabilityMiddleware(), _Context("task_get"), call_next)
    payload = json.loads(capsys.readouterr().err.strip())
    assert "error" not in payload
    assert "error_type" not in payload
    assert "refused" not in payload


def test_tool_is_bound_for_nested_log_lines(capsys):
    # The point of binding a contextvar rather than passing a logger down: a log
    # line emitted deep inside the tool still says which tool it came from.
    from witan_core.observability.logging import get_logger

    async def call_next(_ctx):
        get_logger("deep.inside").info("nested")
        return None

    _run(ObservabilityMiddleware(), _Context("recall"), call_next)
    lines = [
        json.loads(line)
        for line in capsys.readouterr().err.strip().splitlines()
        if line.strip()
    ]
    nested = next(entry for entry in lines if entry["event"] == "nested")
    assert nested["tool"] == "recall"


def test_binding_is_cleared_after_the_call(capsys):
    async def call_next(_ctx):
        return None

    _run(ObservabilityMiddleware(), _Context("recall"), call_next)
    assert "tool" not in structlog.contextvars.get_contextvars()


def test_binding_is_cleared_even_on_failure():
    async def call_next(_ctx):
        msg = "boom"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom"):
        _run(ObservabilityMiddleware(), _Context("recall"), call_next)
    assert "tool" not in structlog.contextvars.get_contextvars()


def test_unnamed_message_does_not_crash(capsys):
    class _Bare:
        message = None

    async def call_next(_ctx):
        return None

    _run(ObservabilityMiddleware(), _Bare(), call_next)
    assert json.loads(capsys.readouterr().err.strip())["tool"] == "unknown"


class _Token:
    """Stands in for fastmcp's AccessToken — the middleware reads ``claims``."""

    def __init__(self, claims):
        self.claims = claims


def _as_actor(monkeypatch, claims):
    """Make ``get_access_token()`` return a token carrying ``claims``."""
    monkeypatch.setattr(middleware_module, "get_access_token", lambda: _Token(claims))


_TMACEY = {
    "sub": "36615884-fc52-465a-9f1d-9db040495163",
    "preferred_username": "tmacey@mit.edu",
}


def test_deployed_call_logs_who_made_it(capsys, monkeypatch):
    # Acceptance criterion 1: one line answers "who did this" with no second
    # lookup. Before this, the line carried tool/outcome/duration and nothing
    # else, and the only identity anywhere was in omnigraph-server's policy log.
    _as_actor(monkeypatch, _TMACEY)

    async def call_next(_ctx):
        return None

    _run(ObservabilityMiddleware(), _Context("store_merge"), call_next)
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["actor_id"] == "act-36615884-fc52-465a-9f1d-9db040495163"
    assert payload["actor"] == "tmacey"


def test_the_log_line_never_carries_the_email_address(capsys, monkeypatch):
    # The maintainer decision this task turned on (2026-08-07): the handle goes
    # to Loki, the address does not. Asserted on the serialized line, not on the
    # helper, because that is the thing that actually gets shipped.
    _as_actor(monkeypatch, _TMACEY)

    async def call_next(_ctx):
        return None

    _run(ObservabilityMiddleware(), _Context("store_merge"), call_next)
    line = capsys.readouterr().err.strip()
    assert "tmacey@mit.edu" not in line
    assert "mit.edu" not in line


def test_call_without_a_jwt_is_explicitly_local(capsys):
    # Not blank, not omitted, not a guess. Deployed, FastMCP rejects an
    # unauthenticated request before the handler runs, so this is local use.
    async def call_next(_ctx):
        return None

    _run(ObservabilityMiddleware(), _Context("recall"), call_next)
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["actor_id"] == LOCAL_ACTOR
    # Nobody to name — an invented or blank handle would be worse than none.
    assert "actor" not in payload


def test_authenticated_but_unusable_sub_is_not_confused_with_local(capsys, monkeypatch):
    # A malformed token is a bug worth seeing; collapsing it into "local" would
    # hide it among every ordinary local call.
    _as_actor(monkeypatch, {"sub": "  ", "preferred_username": "tmacey@mit.edu"})

    async def call_next(_ctx):
        return None

    _run(ObservabilityMiddleware(), _Context("recall"), call_next)
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["actor_id"] == UNKNOWN_ACTOR
    assert payload["actor"] == "tmacey"


def test_identity_lookup_never_fails_the_tool_call(capsys, monkeypatch):
    # An observability layer that can break a user's write is a worse problem
    # than a log line missing its identity. get_access_token() can raise
    # TypeError on a token shape it cannot convert.
    def _explode():
        msg = "unconvertible token"
        raise TypeError(msg)

    monkeypatch.setattr(middleware_module, "get_access_token", _explode)

    async def call_next(_ctx):
        return "result"

    result = _run(ObservabilityMiddleware(), _Context("memory_store"), call_next)
    assert result == "result"
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["actor_id"] == UNKNOWN_ACTOR


def test_identity_is_bound_for_nested_log_lines(capsys, monkeypatch):
    # Same reason `tool` is bound: a line emitted deep inside a tool — including
    # from a dependency's stdlib logger — should still say who caused it. This
    # is what makes two users' concurrent traffic separable in Loki.
    from witan_core.observability.logging import get_logger

    _as_actor(monkeypatch, _TMACEY)

    async def call_next(_ctx):
        get_logger("deep.inside").info("nested")
        return None

    _run(ObservabilityMiddleware(), _Context("store_merge"), call_next)
    lines = [
        json.loads(line)
        for line in capsys.readouterr().err.strip().splitlines()
        if line.strip()
    ]
    nested = next(entry for entry in lines if entry["event"] == "nested")
    assert nested["actor_id"] == "act-36615884-fc52-465a-9f1d-9db040495163"
    assert nested["actor"] == "tmacey"


def test_identity_binding_is_cleared_after_the_call(monkeypatch):
    # A leaked actor binding would misattribute the *next* call on this thread —
    # the exact failure mode that matters once more than one user is served.
    _as_actor(monkeypatch, _TMACEY)

    async def call_next(_ctx):
        return None

    _run(ObservabilityMiddleware(), _Context("store_merge"), call_next)
    remaining = structlog.contextvars.get_contextvars()
    assert "actor_id" not in remaining
    assert "actor" not in remaining


def test_identity_binding_is_cleared_even_on_failure(monkeypatch):
    _as_actor(monkeypatch, _TMACEY)

    async def call_next(_ctx):
        msg = "boom"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom"):
        _run(ObservabilityMiddleware(), _Context("store_merge"), call_next)
    remaining = structlog.contextvars.get_contextvars()
    assert "actor_id" not in remaining
    assert "actor" not in remaining


def test_fastmcp_still_names_the_class_this_way():
    # The middleware classifies elicitations by class name, to avoid importing a
    # fastmcp internal that already moved once between 3.4.x and 4.x. That trade
    # is only safe while the name itself is stable, so pin it: an upstream rename
    # must fail here rather than silently reclassify every elicitation as "ok".
    from fastmcp.tools.base import InputRequiredToolResult as Upstream

    assert Upstream.__name__ == "InputRequiredToolResult"


# ── Through a real FastMCP server ────────────────────────────────────────────
# The tests above drive `on_call_tool` directly, which is the right unit for
# the middleware's own logic but is NOT the shape production produces:
# `FastMCP.call_tool` sits INSIDE the middleware chain and re-raises anything
# that is not a `FastMCPError` as `ToolError(f"Error calling tool ...: {e}")`.
# So `error_type` is the real class only for a FastMCPError. These drive the
# whole server so that claim is tested rather than assumed.


def _server_with_tools():
    """A FastMCP server carrying the middleware and three failing tools."""
    from fastmcp import FastMCP

    from witan_core.refusal import Refusal

    class ClusterGraphMissing(RuntimeError, Refusal):
        pass

    mcp = FastMCP("test")
    mcp.add_middleware(ObservabilityMiddleware())

    @mcp.tool
    def refuses() -> str:
        msg = "'x' code graph is not served by the omnigraph-server"
        raise ClusterGraphMissing(msg)

    @mcp.tool
    def breaks() -> str:
        msg = "underlying omnigraph failure"
        raise RuntimeError(msg)

    @mcp.tool
    def takes_a_string(content: str) -> str:
        return content

    return mcp


def _stderr_payloads(capsys):
    """Every JSON log line just written to stderr."""
    return [
        json.loads(line)
        for line in capsys.readouterr().err.strip().splitlines()
        if line.strip().startswith("{")
    ]


def _call(mcp, name, arguments=None):
    configure_logging(log_format="json", level="INFO", force=True)

    async def drive():
        return await mcp.call_tool(name, arguments or {})

    return asyncio.run(drive())


def test_a_refusal_keeps_its_class_through_the_server(capsys):
    mcp = _server_with_tools()
    with pytest.raises(Exception, match="not served"):
        _call(mcp, "refuses")
    payload = next(p for p in _stderr_payloads(capsys) if p["event"] == "mcp.tool_call")
    assert payload["error_type"] == "ClusterGraphMissing"
    assert payload["refused"] is True
    assert "is not served by the omnigraph-server" in payload["error"]


def test_an_ordinary_exception_arrives_already_wrapped(capsys):
    # Documents the limit the class docstring now states: fastmcp re-raises a
    # non-FastMCPError as ToolError before this middleware sees it, so the
    # class is lost and only the message survives.
    mcp = _server_with_tools()
    with pytest.raises(Exception, match="underlying omnigraph failure"):
        _call(mcp, "breaks")
    payload = next(p for p in _stderr_payloads(capsys) if p["event"] == "mcp.tool_call")
    assert payload["error_type"] == "ToolError"
    assert payload["refused"] is False
    assert "underlying omnigraph failure" in payload["error"]


def test_a_bad_argument_never_puts_the_caller_s_value_in_the_log(capsys):
    # THE LEAK THIS GUARDS. pydantic renders a rejected field as
    # `input_value=<what the caller sent>`, and fastmcp's ValidationError IS
    # that string. Logging it would ship tool arguments to Loki. fastmcp keeps
    # input out of its own line for the same reason
    # (`_validation_error_summary`, "never input-derived validation details")
    # while still telling the client.
    mcp = _server_with_tools()
    sensitive = "ping tmacey@mit.edu about the outage"
    configure_logging(log_format="json", level="INFO", force=True)

    async def drive():
        return await mcp.call_tool("takes_a_string", {"content": {"text": sensitive}})

    with pytest.raises(Exception):
        asyncio.run(drive())
    written = capsys.readouterr().err
    assert sensitive not in written
    assert "tmacey@mit.edu" not in written
    assert "input_value" not in written
    payload = next(
        json.loads(line)
        for line in written.strip().splitlines()
        if line.strip().startswith("{") and '"mcp.tool_call"' in line
    )
    assert payload["outcome"] == "error"
    assert payload["error_withheld"] is True
    assert "error" not in payload


def test_a_withheld_validation_error_still_says_what_kind(capsys):
    # Withholding the message must not mean withholding the diagnosis. fastmcp
    # computes this same summary but logs it on its own non-propagating logger,
    # so it lands beside our JSON as unparsed text rather than in a field a
    # query can reach. We compute it ourselves for that reason.
    mcp = _server_with_tools()
    with pytest.raises(Exception):
        _call(mcp, "takes_a_string", {"content": {"text": "whatever"}})
    payload = next(p for p in _stderr_payloads(capsys) if p["event"] == "mcp.tool_call")
    assert payload["error_withheld"] is True
    assert payload["error_count"] == 1
    assert payload["error_types"] == ["string_type"]


def test_a_model_failing_inside_a_tool_body_is_withheld_too(capsys):
    # A bare pydantic ValidationError reaches the middleware unwrapped and means
    # a model failed INSIDE the body, so the data in its message is upstream's
    # rather than the caller's. Withheld all the same: from here the two differ
    # only in whose data it is, and neither belongs in Loki by default.
    import pydantic

    class Upstream(pydantic.BaseModel):
        n: int

    async def call_next(_ctx):
        Upstream(n="SENSITIVE-ROW-VALUE")

    with pytest.raises(pydantic.ValidationError):
        _run(ObservabilityMiddleware(), _Context("code_store_load"), call_next)
    written = capsys.readouterr().err
    assert "SENSITIVE-ROW-VALUE" not in written
    payload = next(
        json.loads(line)
        for line in written.strip().splitlines()
        if line.strip().startswith("{") and '"mcp.tool_call"' in line
    )
    assert payload["error_type"] == "ValidationError"
    assert payload["error_withheld"] is True
    assert payload["error_count"] == 1
    assert "error" not in payload


def test_an_exception_whose_message_explodes_does_not_replace_it(capsys):
    # _error_fields runs inside `except BaseException`, so anything escaping it
    # would hand the caller a server fault in place of their real failure. The
    # partially built result is kept, so `refused` survives.
    from witan_core.refusal import Refusal

    class Exploding(RuntimeError, Refusal):
        def __str__(self):
            raise KeyboardInterrupt

    async def call_next(_ctx):
        raise Exploding

    with pytest.raises(Exploding):
        _run(ObservabilityMiddleware(), _Context("recall"), call_next)
    payload = next(p for p in _stderr_payloads(capsys) if p["event"] == "mcp.tool_call")
    assert payload["error_type"] == "Exploding"
    assert payload["refused"] is True
    assert payload["error_undescribable"] is True
    assert "error" not in payload


def test_a_custom_validation_code_is_not_logged_verbatim(capsys):
    # A validator may build its PydanticCustomError `type` out of the value it
    # just rejected, so the CODE is caller-controlled too and the summary would
    # leak by the back door. fastmcp allowlists against pydantic's own
    # ErrorType literals for this reason; so do we. No in-repo validator does
    # this today, which is what makes it worth a test rather than a comment.
    from typing import Annotated

    from fastmcp import FastMCP
    from pydantic import AfterValidator
    from pydantic_core import PydanticCustomError

    def reject_loudly(value: str) -> str:
        raise PydanticCustomError("leaked_" + value, "nope")

    mcp = FastMCP("test")
    mcp.add_middleware(ObservabilityMiddleware())

    @mcp.tool
    def checked(value: Annotated[str, AfterValidator(reject_loudly)]) -> str:
        return value

    configure_logging(log_format="json", level="INFO", force=True)

    async def drive():
        return await mcp.call_tool("checked", {"value": "SECRET-TOKEN"})

    with pytest.raises(Exception):
        asyncio.run(drive())
    written = capsys.readouterr().err
    assert "SECRET-TOKEN" not in written
    payload = next(
        json.loads(line)
        for line in written.strip().splitlines()
        if line.strip().startswith("{") and '"mcp.tool_call"' in line
    )
    assert payload["error_types"] == [middleware_module._CUSTOM_VALIDATION_CODE]


def test_a_builtin_validation_code_survives_the_allowlist(capsys):
    # The allowlist must not flatten everything to custom_error, or the summary
    # stops being a diagnosis.
    mcp = _server_with_tools()
    with pytest.raises(Exception):
        _call(mcp, "takes_a_string", {"content": {"text": "whatever"}})
    payload = next(p for p in _stderr_payloads(capsys) if p["event"] == "mcp.tool_call")
    assert payload["error_types"] == ["string_type"]


def test_a_broken_describer_is_distinguishable_from_a_withheld_message(capsys):
    # error_withheld is a decision; error_undescribable is a breakage. One
    # field for both would make them indistinguishable in a Loki filter.
    from witan_core.refusal import Refusal

    class Exploding(RuntimeError, Refusal):
        def __str__(self):
            raise KeyboardInterrupt

    async def call_next(_ctx):
        raise Exploding

    with pytest.raises(Exploding):
        _run(ObservabilityMiddleware(), _Context("recall"), call_next)
    payload = next(p for p in _stderr_payloads(capsys) if p["event"] == "mcp.tool_call")
    assert payload["error_undescribable"] is True
    assert "error_withheld" not in payload
