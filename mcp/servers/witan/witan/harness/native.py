"""What every native CLI adapter shares: ids, refs, resume checks, clipping.

A native adapter only supplies its command lines and its codec. Starting,
resuming and the same-host check are identical for every harness that takes
its session id on the command line.
"""

from __future__ import annotations

import json
import uuid
from abc import ABC, abstractmethod
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar

from ..session_state import SESSION_ID_ENV_VARS
from .base import Capability, Codec, ResumeUnavailable, SessionRef, SessionSpec
from .transport import (
    EventLog,
    StdioTransport,
    SubprocessSession,
    agent_environ,
    this_host,
)

CLIP_CHARS = 4000
"""The most characters of tool input or output an event keeps. The event log
is for watching a worker, not a transcript; the harness keeps the transcript."""


def clip(value: str) -> str:
    if len(value) <= CLIP_CHARS:
        return value
    return value[:CLIP_CHARS] + f"... [{len(value) - CLIP_CHARS} more characters]"


def clip_raw(value: Any) -> Any:
    """``value`` if its JSON fits in :data:`CLIP_CHARS`, else a size marker."""
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded) <= CLIP_CHARS:
        return value
    return {"witan_clipped_chars": len(encoded)}


def absolute(path: str, cwd: Path) -> str:
    """ACP tool locations are absolute; harnesses report some paths relative."""
    return str(cwd / path) if not Path(path).is_absolute() else path


class CliAdapter(ABC):
    """A harness run as ``<cli> --print`` with JSON lines on stdout.

    :param executable: The harness binary, looked up on ``PATH`` if bare.
    """

    name: ClassVar[str]
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.ONESHOT, Capability.RESUME, Capability.EVENTS}
    )
    parent_session_env: ClassVar[tuple[str, ...]] = ()
    """Variables a parent session of this harness exports to its children.
    Inherited, they would tie the worker to the session that started the
    runner, so they are removed from the worker's environment along with
    every agent session id variable; a runner names the worker's own session
    in ``SessionSpec.env``."""

    default_executable: ClassVar[str]

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or self.default_executable

    @abstractmethod
    def start_argv(self, spec: SessionSpec, session_id: str) -> list[str]: ...

    @abstractmethod
    def resume_argv(self, spec: SessionSpec, session_id: str) -> list[str]: ...

    @abstractmethod
    def codec(self, cwd: Path) -> Codec: ...

    def stdin_prompt(self, spec: SessionSpec) -> str | None:
        """The prompt to write to stdin, or ``None`` when it goes in argv."""
        return None

    def start(self, spec: SessionSpec) -> SubprocessSession:
        cwd = spec.cwd.resolve()
        ref = SessionRef(
            harness=self.name,
            harness_session_ref=spec.session_id or str(uuid.uuid4()),
            host=this_host(),
            cwd=cwd,
        )
        return self._launch(
            self.start_argv(spec, ref.harness_session_ref), spec, ref, resumed=False
        )

    def resume(
        self, ref: SessionRef, message: str, spec: SessionSpec
    ) -> SubprocessSession:
        if ref.harness != self.name:
            raise ResumeUnavailable(
                f"{ref.harness} session given to the {self.name} adapter"
            )
        if ref.host != this_host():
            raise ResumeUnavailable(f"session ran on {ref.host}, not {this_host()}")
        if spec.cwd.resolve() != ref.cwd:
            raise ResumeUnavailable(
                f"session ran in {ref.cwd}, not {spec.cwd.resolve()}"
            )
        spec = replace(spec, prompt=message)
        return self._launch(
            self.resume_argv(spec, ref.harness_session_ref), spec, ref, resumed=True
        )

    def _launch(
        self, argv: list[str], spec: SessionSpec, ref: SessionRef, *, resumed: bool
    ) -> SubprocessSession:
        # The log opens first, so a log that can't be written stops the run
        # before there is a harness editing the worktree with no handle to it.
        log = EventLog(spec.event_log, meta={"harness": self.name, **spec.meta})
        transport: StdioTransport | None = None
        try:
            transport = StdioTransport(
                argv,
                cwd=ref.cwd,
                env=agent_environ(
                    spec.env,
                    [*SESSION_ID_ENV_VARS, *self.parent_session_env, *spec.unset_env],
                ),
                stdin_text=self.stdin_prompt(spec),
            )
            return SubprocessSession(
                ref=ref,
                transport=transport,
                codec=self.codec(ref.cwd),
                log=log,
                prompt=spec.prompt,
                resumed=resumed,
            )
        except BaseException:
            if transport is not None:
                transport.kill()
            log.close()
            raise
