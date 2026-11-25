"""The stdio transport, the per-session event log, and the session lifecycle.

A native adapter is a command line plus a codec. Everything else (spawning
the harness, draining its pipes, writing the event log, resolving the exit)
is the same for every harness, and lives here.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import threading
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from .acp import (
    PROTOCOL_VERSION,
    Notice,
    SessionInfoUpdate,
    SessionNotification,
    SessionUpdate,
    UserMessageChunk,
    text,
    witan_meta,
)
from .base import Codec, Exit, SessionRef, Unsupported

STDERR_TAIL_LINES = 50
UNPARSED_LINE_CHARS = 2000


def agent_environ(env: Mapping[str, str], unset_env: Sequence[str]) -> dict[str, str]:
    """This process's environment with ``unset_env`` removed and ``env`` set over it."""
    return {k: v for k, v in os.environ.items() if k not in unset_env} | dict(env)


def this_host() -> str:
    return socket.gethostname()


class StdioTransport:
    """A harness subprocess whose stdout carries one JSON object per line.

    stderr is drained on a thread into a bounded tail, so a chatty harness
    can't stall on a full pipe while the caller reads stdout.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        stdin_text: str | None = None,
    ) -> None:
        self.argv = list(argv)
        self._proc = subprocess.Popen(
            self.argv,
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self._stderr: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self._threads = [threading.Thread(target=self._drain_stderr, daemon=True)]
        if stdin_text is not None:
            self._threads.append(
                threading.Thread(
                    target=self._feed_stdin, args=(stdin_text,), daemon=True
                )
            )
        for thread in self._threads:
            thread.start()

    @property
    def pid(self) -> int:
        return self._proc.pid

    def _drain_stderr(self) -> None:
        assert self._proc.stderr is not None
        for line in self._proc.stderr:
            self._stderr.append(line)

    def _feed_stdin(self, value: str) -> None:
        assert self._proc.stdin is not None
        # A harness that exits before reading its prompt closes the pipe; its
        # exit status and stderr say why, so the broken pipe adds nothing.
        try:
            self._proc.stdin.write(value)
            self._proc.stdin.close()
        except BrokenPipeError:
            pass

    def lines(self) -> Iterator[str]:
        assert self._proc.stdout is not None
        yield from self._proc.stdout

    def wait(self) -> int:
        returncode = self._proc.wait()
        for thread in self._threads:
            thread.join()
        return returncode

    def kill(self) -> int:
        """Kill the harness and reap it, for a session that can no longer drain it."""
        self._proc.kill()
        return self.wait()

    @property
    def stderr_tail(self) -> str:
        return "".join(self._stderr)


class EventLog:
    """Appends a session's notifications to a JSONL file, one per line.

    Each line is flushed as it is written, because ``witan fanout status``
    reads a running worker's last activity from this file. The file is the
    only copy of a run's notifications: :meth:`follow` reads them back from
    it, so a run nobody watches holds none of them in memory.

    The log is owner-only, because it carries the prompt and clipped tool
    input and output.
    """

    RESERVED_META = frozenset({"seq", "ts"})

    def __init__(self, path: Path, *, meta: Mapping[str, Any]) -> None:
        if reserved := self.RESERVED_META & meta.keys():
            raise ValueError(f"event log meta cannot set {sorted(reserved)}")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._meta = dict(meta)
        fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        status = os.fstat(fd)
        if mode := stat.S_IMODE(status.st_mode) & 0o077:
            os.close(fd)
            raise PermissionError(
                f"refusing to append to event log {path}: its mode "
                f"{stat.S_IMODE(status.st_mode):#o} grants access to other users "
                f"({mode:#o}); restrict it to 0o600"
            )
        # A resume appends to its start's log: numbering carries on from it,
        # and this run's notifications start at its current end.
        self.start_offset = status.st_size
        with path.open(encoding="utf-8") as existing:
            self._seq = sum(1 for line in existing if line.strip())
        self._file: TextIO = os.fdopen(fd, "a", encoding="utf-8")
        self._changed = threading.Condition()
        self._writes = 0
        self._end_offset: int | None = None

    def write(self, session_id: str, update: SessionUpdate) -> SessionNotification:
        self._seq += 1
        notification = SessionNotification(
            session_id=session_id,
            update=update,
            meta=witan_meta(
                **self._meta, seq=self._seq, ts=datetime.now(UTC).isoformat()
            ),
        )
        self._file.write(json.dumps(notification.wire(), ensure_ascii=False) + "\n")
        self._file.flush()
        with self._changed:
            self._writes += 1
            self._changed.notify_all()
        return notification

    def close(self) -> None:
        end = os.fstat(self._file.fileno()).st_size
        self._file.close()
        with self._changed:
            self._end_offset = end
            self._changed.notify_all()

    def follow(self) -> Iterator[SessionNotification]:
        """This run's notifications, read back from the file as they are
        written, ending where this run closed the log (a resume appends
        after it)."""
        with self.path.open("rb") as stream:
            stream.seek(self.start_offset)
            partial = b""
            while True:
                # Taken before reading, so a write that lands after the read
                # reaches EOF still changes the count and ends the wait.
                with self._changed:
                    seen, end = self._writes, self._end_offset
                while (end is None or stream.tell() < end) and (
                    chunk := stream.readline()
                ):
                    partial += chunk
                    if partial.endswith(b"\n"):
                        if partial.strip():
                            yield SessionNotification.model_validate_json(partial)
                        partial = b""
                if end is not None:
                    return
                with self._changed:
                    while self._writes == seen and self._end_offset is None:
                        self._changed.wait()


def read_event_log(path: Path) -> list[SessionNotification]:
    """Every notification in an event log, in the order written."""
    # Not splitlines(): it also splits on U+2028 and the like, which the
    # writer leaves unescaped inside JSON strings.
    with path.open(encoding="utf-8") as stream:
        return [
            SessionNotification.model_validate_json(line)
            for line in stream
            if line.strip()
        ]


class SubprocessSession:
    """One harness run: a pump thread drains the transport through the codec.

    The pump writes each notification to the event log as it arrives, so the
    harness never stalls on a full stdout pipe and the log stays current
    whether or not anyone iterates ``events()``. When stdout closes, the pump
    reaps the harness and writes the ``exited`` update, so a log reader sees
    the end without the runner calling ``wait()``. ``events()`` reads the log
    back, yielding every notification of this run from the start through
    ``exited``, so nothing is held in memory for a runner that only calls
    ``wait()``.
    """

    def __init__(
        self,
        *,
        ref: SessionRef,
        transport: StdioTransport,
        codec: Codec,
        log: EventLog,
        prompt: str,
        resumed: bool,
    ) -> None:
        self.ref = ref
        self._transport = transport
        self._codec = codec
        self._log = log
        self._exit: Exit | None = None
        self._failure: Exception | None = None
        self._emit(
            SessionInfoUpdate(
                meta=witan_meta(
                    lifecycle="started",
                    protocol_version=PROTOCOL_VERSION,
                    host=ref.host,
                    cwd=str(ref.cwd),
                    resumed=resumed,
                    pid=transport.pid,
                )
            )
        )
        self._emit(UserMessageChunk(content=text(prompt)))
        self._pump = threading.Thread(target=self._drain, daemon=True)
        self._pump.start()

    @property
    def event_log(self) -> Path:
        return self._log.path

    def _emit(self, update: SessionUpdate) -> None:
        self._log.write(self.ref.harness_session_ref, update)

    def _drain(self) -> None:
        try:
            for line in self._transport.lines():
                if not line.strip():
                    continue
                updates = self._decode(line)
                # The harness's own report of its session id wins over the
                # one passed on the command line (e.g. a forked resume).
                reported = self._codec.session_id
                if reported and reported != self.ref.harness_session_ref:
                    self.ref = replace(self.ref, harness_session_ref=reported)
                for update in updates:
                    self._emit(update)
            self._finish()
        except Exception as exc:  # noqa: BLE001 -- re-raised by wait()
            # Nothing drains stdout any more, so the harness would block on a
            # full pipe; wait() raises this instead of returning an Exit.
            self._failure = exc
            self._transport.kill()
        finally:
            self._log.close()

    def _finish(self) -> None:
        returncode = self._transport.wait()
        codec = self._codec
        self._exit = Exit(
            ref=self.ref,
            returncode=returncode,
            stop_reason=codec.stop_reason,
            error=codec.error,
            usage=dict(codec.usage),
            stderr_tail=self._transport.stderr_tail,
        )
        self._emit(
            SessionInfoUpdate(
                meta=witan_meta(
                    lifecycle="exited",
                    returncode=returncode,
                    stop_reason=codec.stop_reason,
                    error=codec.error,
                    usage=dict(codec.usage),
                )
            )
        )

    def events(self) -> Iterator[SessionNotification]:
        return self._log.follow()

    def _decode(self, line: str) -> list[SessionUpdate]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            record = None
        if not isinstance(record, dict):
            return [
                Notice(
                    severity="warning",
                    title="unparsed harness output",
                    description=line.rstrip("\n")[:UNPARSED_LINE_CHARS],
                )
            ]
        try:
            return self._codec.decode(record)
        # One record the codec can't read (e.g. a TodoWrite with a status
        # outside ACP's) must not stop the pump that drains the harness.
        except Exception as exc:  # noqa: BLE001 -- logged as a notice
            return [
                Notice(
                    severity="error",
                    title="harness output the codec could not decode",
                    description=f"{type(exc).__name__}: {exc}"[:UNPARSED_LINE_CHARS],
                )
            ]

    def send(self, message: str) -> None:
        raise Unsupported(f"{self.ref.harness} has no live input in one-shot mode")

    def wait(self) -> Exit:
        self._pump.join()
        if self._failure is not None:
            raise self._failure
        assert self._exit is not None
        return self._exit
