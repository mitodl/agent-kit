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


#########################################
#   Trace-context propagation            #
#########################################
# ToolHive puts the caller's W3C context in the MCP `_meta` object, not in an
# HTTP header, and nothing in this process reads headers. These tests pin the
# NESTING, not "a span was emitted": a parentless root span satisfies the weaker
# assertion while leaving the proxy -> witan boundary unmeasurable, which is
# exactly the defect they exist to stop recurring.

_CALLER_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
_CALLER_SPAN_ID = "00f067aa0ba902b7"


class _MetaMessage(_Message):
    def __init__(self, name, meta):
        super().__init__(name)
        self.meta = meta


class _MetaContext:
    def __init__(self, name, meta):
        self.message = _MetaMessage(name, meta)


def _record_spans(monkeypatch, sampler=None):
    """Point the middleware at an in-memory exporter, and return the store."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=sampler) if sampler else TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # Bound to the middleware directly rather than through the global provider:
    # `trace.set_tracer_provider` refuses a second call in one process, so going
    # global would make these tests order-dependent.
    monkeypatch.setattr(
        middleware_module, "_tracer", lambda: provider.get_tracer("witan-test")
    )
    return exporter


def _traceparent_context(sampled="01"):
    return _MetaContext(
        "memory_search",
        {"traceparent": f"00-{_CALLER_TRACE_ID}-{_CALLER_SPAN_ID}-{sampled}"},
    )


async def _ok(_ctx):
    return None


def test_span_nests_under_the_callers_meta_trace_context(monkeypatch):
    exporter = _record_spans(monkeypatch)

    _run(ObservabilityMiddleware(), _traceparent_context(), _ok)

    (span,) = exporter.get_finished_spans()
    assert format(span.context.trace_id, "032x") == _CALLER_TRACE_ID, (
        "span started its own trace instead of joining ToolHive's"
    )
    assert span.parent is not None, "span is a root — it did not nest"
    assert format(span.parent.span_id, "016x") == _CALLER_SPAN_ID


def test_sampling_decision_is_inherited_not_rerolled(monkeypatch):
    # The second half of the same defect: with no parent,
    # `parentbased_traceidratio` falls back to its ROOT sampler and re-rolls the
    # ratio locally, so witan dropped traces ToolHive had already decided to
    # keep — which is why zero of six QA calls produced a trace.
    #
    # ★ THE SAMPLER HERE IS THE WHOLE TEST. Under the SDK default every span is
    # sampled, so this passes with or without the parent and proves nothing (it
    # did exactly that when first written). `ParentBased(root=ALWAYS_OFF)` is
    # the production shape reduced to its decisive case: inherit and the span
    # lives, re-roll at the root and it is dropped.
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ParentBased

    exporter = _record_spans(monkeypatch, sampler=ParentBased(root=ALWAYS_OFF))

    _run(ObservabilityMiddleware(), _traceparent_context(), _ok)

    spans = exporter.get_finished_spans()
    assert spans, "the parent's sampled=01 decision was not inherited"
    assert spans[0].context.trace_flags.sampled is True


def test_root_sampler_still_applies_when_there_is_no_parent(monkeypatch):
    # The converse, so the test above cannot pass by the sampler being ignored
    # altogether: with no `_meta` there is nothing to inherit, and the root
    # sampler must still get its say.
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ParentBased

    exporter = _record_spans(monkeypatch, sampler=ParentBased(root=ALWAYS_OFF))

    _run(ObservabilityMiddleware(), _Context("memory_search"), _ok)

    assert exporter.get_finished_spans() == ()


def test_no_meta_still_emits_a_root_span(monkeypatch):
    # stdio and local CLI use carry no `_meta`. That must degrade to an ordinary
    # root span, never an error.
    exporter = _record_spans(monkeypatch)

    _run(ObservabilityMiddleware(), _Context("memory_search"), _ok)

    (span,) = exporter.get_finished_spans()
    assert span.name == "mcp.tool/memory_search"
    assert span.parent is None


def test_unusable_meta_does_not_break_the_call(monkeypatch):
    # A `_meta` with no traceparent, or a malformed one, must not raise: the
    # tool call matters more than its telemetry.
    exporter = _record_spans(monkeypatch)
    calls = []

    async def call_next(_ctx):
        calls.append(1)
        return None

    _run(
        ObservabilityMiddleware(),
        _MetaContext("memory_search", {"progressToken": "p", "traceparent": "junk"}),
        call_next,
    )

    assert calls == [1]
    (span,) = exporter.get_finished_spans()
    assert span.parent is None
