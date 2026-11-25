"""Harness adapters: start, watch and resume a coding agent session.

Every adapter normalizes its harness's output into ACP v1 session updates
(:mod:`.acp`) and appends them to a per-session JSONL event log. Native
adapters drive a harness's own CLI; an ACP adapter can drive any agent that
speaks the protocol and relay its updates as they are.
"""

from __future__ import annotations

from .base import (
    AgentSession,
    Capability,
    Exit,
    HarnessAdapter,
    ResumeUnavailable,
    SessionRef,
    SessionSpec,
    Unsupported,
)
from .claude import ClaudeAdapter
from .native import CliAdapter
from .pi import PiAdapter
from .transport import read_event_log

ADAPTERS: dict[str, type[CliAdapter]] = {
    ClaudeAdapter.name: ClaudeAdapter,
    PiAdapter.name: PiAdapter,
}
"""Native adapters by the name ``WorkflowSession.harness`` records."""

__all__ = [
    "ADAPTERS",
    "AgentSession",
    "Capability",
    "ClaudeAdapter",
    "Exit",
    "HarnessAdapter",
    "PiAdapter",
    "ResumeUnavailable",
    "SessionRef",
    "SessionSpec",
    "Unsupported",
    "read_event_log",
]
