"""The harness adapter interface: what a runner needs from any coding harness.

A runner starts a session in a worktree, drains its events into a log, learns
the harness's own session id, and later resumes that session on the same host.
Each adapter declares which of those it supports (:class:`Capability`), so a
streaming or networked mode can be added without reshaping the interface.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from .acp import SessionNotification, SessionUpdate, StopReason


class Capability(StrEnum):
    ONESHOT = "oneshot"
    """Run one prompt to completion."""
    RESUME = "resume"
    """Continue a session by its ref, on the host that ran it."""
    EVENTS = "events"
    """Machine-readable events while the session runs."""
    LIVE_INPUT = "live_input"
    """Send messages into a running session."""
    ATTACH = "attach"
    """Reach the session over the network."""


class Unsupported(Exception):
    """The adapter lacks the capability an operation needs."""


class ResumeUnavailable(Exception):
    """The ref names a session this host cannot resume (another host or cwd)."""


@dataclass(frozen=True)
class SessionRef:
    """Where a harness session lives, so it can be resumed.

    Field names match ``WorkflowSession``'s ``harness`` and
    ``harness_session_ref``. A harness keeps its transcripts per host and per
    working directory (Claude Code under ``~/.claude/projects/<cwd>``, Pi
    under its session directory for ``cwd``), so both are part of the ref.
    """

    harness: str
    harness_session_ref: str
    host: str
    cwd: Path


@dataclass(frozen=True)
class SessionSpec:
    """What to run: one prompt in one worktree.

    Permissions come from each harness's launch flags, scoped to ``cwd``; see
    the adapter for what that means for its harness.

    :param prompt: The message the session starts with.
    :param cwd: The worktree the harness runs in.
    :param event_log: The JSONL file normalized events are appended to.
    :param model: The harness's model name or alias, or ``None`` for its default.
    :param session_id: The harness session id to use, for a harness that takes
        one on the command line. ``None`` mints a fresh UUID.
    :param allowed_tools: The harness's own tool rule list, whose meaning is
        the harness's: Claude adds these to what it allows without asking
        (e.g. ``Bash(git commit:*)``), while Pi enables only these tools
        (e.g. ``read,edit,write``). Empty keeps the harness's defaults.
    :param env: Variables to set over the inherited environment.
    :param unset_env: Inherited variables to remove before ``env`` is applied.
    :param meta: witan fields recorded in every event's ``_meta`` (e.g. the
        task slug and runner).
    """

    prompt: str
    cwd: Path
    event_log: Path
    model: str | None = None
    session_id: str | None = None
    allowed_tools: Sequence[str] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    unset_env: Sequence[str] = ()
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Exit:
    """How a session ended.

    :param returncode: The harness process's exit status.
    :param stop_reason: The last turn's ACP stop reason, when the harness
        reported one.
    :param error: The harness's error message, when it reported one. A harness
        can exit 0 after a failed model call (Pi does on an HTTP 429), so this,
        not ``returncode``, is what says the turn failed.
    :param usage: Token counts and cost as the harness reported them.
    :param stderr_tail: The last lines the harness wrote to stderr.
    """

    ref: SessionRef
    returncode: int
    stop_reason: StopReason | None
    error: str | None
    usage: Mapping[str, Any]
    stderr_tail: str

    @property
    def ok(self) -> bool:
        return (
            self.returncode == 0
            and self.error is None
            and self.stop_reason == "end_turn"
        )


class Codec(Protocol):
    """Translates one harness's JSON lines into ACP session updates.

    One instance per session: it carries state across lines (the session id,
    open tool calls, the last stop reason).
    """

    session_id: str | None
    stop_reason: StopReason | None
    error: str | None
    usage: dict[str, Any]

    def decode(self, record: dict[str, Any]) -> list[SessionUpdate]: ...


class AgentSession(Protocol):
    ref: SessionRef

    def events(self) -> Iterator[SessionNotification]: ...

    def send(self, message: str) -> None:
        """Send a message into the running session.

        :raises Unsupported: without :attr:`Capability.LIVE_INPUT`.
        """
        ...

    def wait(self) -> Exit: ...


class HarnessAdapter(Protocol):
    name: str
    capabilities: frozenset[Capability]

    def start(self, spec: SessionSpec) -> AgentSession: ...

    def resume(self, ref: SessionRef, message: str, spec: SessionSpec) -> AgentSession:
        """Continue ``ref`` with ``message``, which replaces ``spec.prompt``.

        :raises ResumeUnavailable: when ``ref`` belongs to another host or
            working directory.
        """
        ...
