"""A per-deployment journal of writes held back while the deployment was down.

:class:`~witan_core.remote.proxy.RemoteMCPProxy` appends a write here when the
connection to the deployment could not be opened (so the request provably never
left this machine) and replays the journal, oldest first, at the start of the
next call whose connection does open. Before this, every agent that hit an
outage invented its own scratchpad buffer and replayed it by hand, or not at
all.

One file per deployment, named by a digest of its URL, so an entry can never be
replayed into a different graph than the one it was meant for. It lives under
the XDG data home rather than the per-boot state dir because a queued write has
to survive a reboot.

Two locks, because the two jobs have different shapes. The file lock is held
only for the read-modify-write of the file itself, never across network I/O, so
an append never waits on a slow replay. The replay lock is held across the whole
replay and taken non-blocking: a second process (``witan serve`` and a CLI call,
or two coroutines in one server) that finds it held skips replaying rather than
waiting, so no entry is sent twice.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..timeutil import now_iso

__all__ = ["JournalEntry", "WriteJournal", "journal_dir", "journal_path"]

# Hex characters of the URL digest in a journal's file name.
_URL_DIGEST_CHARS = 16


def journal_dir() -> Path:
    """Where every deployment's journal lives.

    Resolved per call, not at import, so a redirected ``XDG_DATA_HOME`` (the
    test harness, a sandbox) is honoured whenever this module was imported.
    """
    data_home = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(data_home) / "witan" / "journal"


def journal_path(remote_url: str) -> Path:
    """The journal file for the deployment at ``remote_url``."""
    digest = hashlib.sha256(remote_url.encode()).hexdigest()[:_URL_DIGEST_CHARS]
    return journal_dir() / f"{digest}.jsonl"


@dataclass(frozen=True)
class JournalEntry:
    """One held-back tool call.

    ``args`` are the caller's keyword arguments with ``None`` dropped, NOT the
    mapped wire arguments: mapping needs the deployment's tool list, and the
    deployment is exactly what could not be reached. ``context`` carries the
    client-side values mapping would have injected (repo, branch, session),
    resolved when the call was made, so a replay from another directory or
    session still sends what the caller meant. ``None`` means "resolve at
    replay", which is what a substitute call built during replay wants.

    ``holder`` is the claim-holder string of the caller, which replay compares
    against the task's assignee to decide whether the write still applies.
    """

    id: str
    ts: str
    tool: str
    args: dict[str, Any]
    context: dict[str, str | None] | None
    holder: str | None


class WriteJournal:
    """The append/replay file for one deployment."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file_lock = path.with_name(path.name + ".lock")
        self._replay_lock = path.with_name(path.name + ".replay.lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._file_lock, "w") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _read(self) -> list[JournalEntry]:
        if not self.path.exists():
            return []
        lines = self.path.read_text().splitlines()
        return [JournalEntry(**json.loads(line)) for line in lines if line.strip()]

    def _write(self, entries: list[JournalEntry]) -> None:
        if not entries:
            self.path.unlink(missing_ok=True)
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text("".join(json.dumps(asdict(e)) + "\n" for e in entries))
        tmp.replace(self.path)

    def has_pending(self) -> bool:
        """Whether anything is queued. A stat, so it is cheap on every call."""
        return self.path.exists() and self.path.stat().st_size > 0

    def entries(self) -> list[JournalEntry]:
        """Every queued entry, oldest first."""
        with self._locked():
            return self._read()

    def append(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        context: dict[str, str | None] | None,
        holder: str | None,
    ) -> tuple[JournalEntry, int]:
        """Queue one call. Returns the entry and how many are now pending."""
        entry = JournalEntry(
            id=uuid.uuid4().hex[:12],
            ts=now_iso(),
            tool=tool,
            args=args,
            context=context,
            holder=holder,
        )
        with self._locked():
            entries = [*self._read(), entry]
            self._write(entries)
        return entry, len(entries)

    def remove(self, entry_id: str) -> bool:
        """Drop one entry by id. Returns whether it was there."""
        with self._locked():
            entries = self._read()
            kept = [e for e in entries if e.id != entry_id]
            self._write(kept)
        return len(kept) != len(entries)

    @contextmanager
    def replaying(self) -> Iterator[bool]:
        """Hold the replay lock if nobody else does. Yields whether it was taken."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._replay_lock, "w") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
