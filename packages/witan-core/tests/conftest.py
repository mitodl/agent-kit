import functools

import pytest
import sentry_sdk
from sentry_sdk.transport import Transport

from witan_core.observability.telemetry import configure_sentry, reset_telemetry


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
