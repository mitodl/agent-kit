"""A designed refusal is not an incident.

It reaches the client as its own message, fastmcp logs it at WARNING, and the
Sentry client ``configure_sentry`` builds never sends an event for it. A genuine
failure out of the same tool still does, which is what keeps this from being a
blanket mute.
"""

import asyncio
import functools
import logging

import pytest
import sentry_sdk
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from sentry_sdk.transport import Transport

from witan_core.identity import ActorTokenMissing
from witan_core.observability.telemetry import configure_sentry, reset_telemetry
from witan_core.omnigraph import (
    AdmissionCapExceeded,
    WriteIndeterminate,
    WriteQueueFull,
)
from witan_core.refusal import Refusal

REFUSALS = [
    (WriteQueueFull, RuntimeError),
    (WriteIndeterminate, RuntimeError),
    (AdmissionCapExceeded, RuntimeError),
    (ActorTokenMissing, LookupError),
]


class _CapturingTransport(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict] = []

    def capture_envelope(self, envelope) -> None:
        event = envelope.get_event()
        if event is not None:
            self.events.append(event)


@pytest.fixture
def sentry_events(monkeypatch):
    """Events the production Sentry setup would send, captured instead."""
    transport = _CapturingTransport()
    monkeypatch.setenv("SENTRY_DSN", "https://abc123@o123456.ingest.sentry.io/123456")
    monkeypatch.setattr(
        sentry_sdk, "init", functools.partial(sentry_sdk.init, transport=transport)
    )
    reset_telemetry()
    assert configure_sentry() is not None
    yield transport.events
    reset_telemetry()


@pytest.fixture
def tool_error_records():
    """fastmcp's "Error calling tool" records. Its logger does not propagate."""
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("fastmcp")
    logger.addHandler(handler)
    yield records
    logger.removeHandler(handler)


def _call_raising(exc: BaseException) -> str:
    mcp = FastMCP("refusal-test")

    @mcp.tool
    def refuse() -> str:
        raise exc

    async def call() -> None:
        async with Client(mcp) as client:
            await client.call_tool("refuse", {})

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(call())
    return str(excinfo.value)


def _levels(records: list[logging.LogRecord]) -> list[int]:
    return [
        r.levelno for r in records if r.getMessage().startswith("Error calling tool")
    ]


@pytest.mark.parametrize(("cls", "builtin"), REFUSALS)
def test_every_refusal_keeps_its_builtin_type_and_logs_at_warning(cls, builtin):
    # The builtin is what the CLI and the retry loops catch these by.
    exc = cls("refused")
    assert isinstance(exc, Refusal)
    assert isinstance(exc, builtin)
    assert exc.log_level == logging.WARNING


@pytest.mark.parametrize(("cls", "_builtin"), REFUSALS)
def test_a_refusal_reaches_the_client_verbatim_without_a_sentry_event(
    cls, _builtin, tool_error_records, sentry_events
):
    message = "NOTHING WAS WRITTEN - retry once the burst clears."
    assert _call_raising(cls(message)) == message
    assert _levels(tool_error_records) == [logging.WARNING]
    assert sentry_events == []


def test_an_unexpected_failure_is_still_an_error(tool_error_records, sentry_events):
    assert "boom" in _call_raising(RuntimeError("boom"))
    assert logging.ERROR in _levels(tool_error_records)
    assert sentry_events
