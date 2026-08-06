"""Tests for the tool-call observability middleware."""

import asyncio
import json

import pytest
import structlog

from witan_core.observability.logging import configure_logging, reset_logging
from witan_core.observability.middleware import ObservabilityMiddleware
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


def test_fastmcp_still_names_the_class_this_way():
    # The middleware classifies elicitations by class name, to avoid importing a
    # fastmcp internal that already moved once between 3.4.x and 4.x. That trade
    # is only safe while the name itself is stable, so pin it: an upstream rename
    # must fail here rather than silently reclassify every elicitation as "ok".
    from fastmcp.tools.base import InputRequiredToolResult as Upstream

    assert Upstream.__name__ == "InputRequiredToolResult"
