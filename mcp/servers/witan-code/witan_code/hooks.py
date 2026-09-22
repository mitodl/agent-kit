"""SessionStart and PostToolUse hook logic, as plain Python.

Ported from the former codegraph-session-init.sh / codegraph-reindex.sh bash
scripts so hook invocation is a portable CLI command everywhere the
`witan-code` binary installs — Windows included, where bash/setsid don't
exist — matching the bare `witan-code inject-context`/`checkpoint` pattern
this package's other two hooks already use (and witan's own `witan
inject-context`/`session-checkpoint`).

ONE WRITER PER CHECKOUT. Every write to a checkout's code graph goes through
the same lock (:func:`witan_code.context._lock_path`): the SessionStart full
index holds it, and so does the drainer that applies per-edit reindexes. The
per-edit hook used to index in the foreground, one process per Edit/Write, so
an agent fanning out N subagents in one worktree put N uncoordinated writers on
one branch view. Against a deployed graph they lost each other's optimistic-
concurrency races until the retry budget ran out (Sentry WITAN-12). The hook
now queues the path and leaves the write to a single detached drainer.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from witan_core import popen_detached
from witan_core.observability import get_logger

from . import indexer
from . import repo as repo_module
from .context import _lock_digest, _lock_path, _project_dir

logger = get_logger("witan.code.hooks")

# The pid of whichever process is doing the indexing, so a lock left behind by
# a killed indexer can be recognised instead of blocking every later write to
# that checkout. Before the drainer existed a stale lock only cost the next
# SessionStart its refresh; now it would silently stop per-edit reindexing too.
_LOCK_PID_FILE = "pid"

# How long a lock with no pid file is trusted. The pid is written a moment
# after the lock is taken (the holder is a child that has to be spawned first),
# so a missing pid is normal for that moment and means a crash only after it.
_UNOWNED_LOCK_GRACE_SECONDS = 60.0

_PENDING_PREFIX = "codegraph-pending-"


def session_init() -> None:
    """SessionStart: seed/refresh the whole repo's code graph in the
    background, at most once across overlapping sessions.

    Best-effort and non-blocking: skips non-git directories, never raises,
    and returns immediately — the actual indexing happens in a fully detached
    child process (see :func:`index_and_unlock`), which is what makes this
    safe to call from a hook that must not block session start.
    """
    project_dir = _project_dir()
    if repo_module.root(project_dir) is None:
        return

    lock = _lock_path(project_dir)
    if not _try_lock(lock):
        return  # another process is already indexing this repo

    try:
        proc = popen_detached(
            [
                sys.executable,
                "-m",
                "witan_code",
                "_index-and-unlock",
                str(project_dir),
                str(lock),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        _unlock(lock)
        return
    _write_lock_pid(lock, proc.pid)


def index_and_unlock(target: Path, lock: Path) -> None:
    """Run by the detached child :func:`session_init` spawns — never called
    directly by a hook. Indexes ``target``, then always releases ``lock``,
    however indexing turns out, so a parse failure can't wedge the lock and
    permanently block future sessions from indexing this repo.

    Edits queued while the full index held the lock are applied afterwards:
    their hooks saw the lock held and left the work to whoever held it.
    """
    try:
        indexer.index_path(target, force=False)
    except Exception:  # noqa: BLE001 — a bad repo must not leave the lock held
        pass
    finally:
        _unlock(lock)
    root = repo_module.root(target)
    if root is not None:
        drain_pending(root)


def reindex_hook(payload: str) -> None:
    """PostToolUse (matcher ``Edit|Write``): reindex the edited file.

    ``payload`` is the raw hook JSON read from stdin. Inside a git checkout
    the path is queued and a detached drainer applies it (see the module
    docstring for why), so the index lands a few seconds after the edit rather
    than before the hook returns. Outside one there is no branch view to
    contend over and the file is indexed in the foreground, as before.

    Best-effort: a missing/malformed payload, an untracked tool, or a parse
    failure all degrade to a silent no-op rather than interrupting the agent.
    """
    try:
        data = json.loads(payload) if payload else {}
    except json.JSONDecodeError:
        return
    tool_input = data.get("tool_input") if isinstance(data, dict) else None
    if not isinstance(tool_input, dict):
        return
    raw = (
        tool_input.get("file_path")
        or tool_input.get("path")
        or tool_input.get("filename")
    )
    if not isinstance(raw, str) or not raw:
        return

    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.is_file():
        return

    root = repo_module.root(path.parent)
    if root is None:
        try:
            indexer.index_path(path, force=False)
        except Exception:  # noqa: BLE001 — a parse failure must not fail the hook
            pass
        return

    _enqueue(root, path)
    # Checked AFTER the enqueue, and the order is what makes the handoff safe:
    # a drainer re-checks the queue after releasing the lock, so a path queued
    # while it held the lock is picked up by that re-check, and one queued
    # after the release finds the lock free here.
    if _lock_held(_lock_path(root)):
        return
    try:
        popen_detached(
            [sys.executable, "-m", "witan_code", "_drain-pending", str(root)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def drain_pending(root: Path) -> None:
    """Apply every queued reindex for the checkout at ``root``, one at a time.

    Run by the detached child :func:`reindex_hook` spawns, and after a
    SessionStart full index. Returns at once if another process holds the
    lock: that process drains the queue before it lets go.
    """
    lock = _lock_path(root)
    while _has_pending(root):
        if not _try_lock(lock):
            return
        _write_lock_pid(lock, os.getpid())
        try:
            for path in _take_pending(root):
                if not path.is_file():
                    continue  # deleted or moved since the edit that queued it
                try:
                    indexer.index_path(path, force=False)
                except Exception as exc:  # noqa: BLE001 — one file must not stop the rest
                    logger.warning(
                        "witan.code.hooks.reindex_failed",
                        path=str(path),
                        error=str(exc),
                    )
        finally:
            _unlock(lock)


# ── Pending queue ─────────────────────────────────────────────────────────────


def _pending_path(root: Path) -> Path:
    tmp = Path(os.environ.get("TMPDIR", "/tmp"))
    return tmp / f"{_PENDING_PREFIX}{_lock_digest(root)}"


def _enqueue(root: Path, path: Path) -> None:
    with _pending_path(root).open("a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        fh.write(f"{path}\n")


def _has_pending(root: Path) -> bool:
    try:
        return _pending_path(root).stat().st_size > 0
    except FileNotFoundError:
        return False


def _take_pending(root: Path) -> list[Path]:
    """Empty the queue and return its paths, each once, in first-queued order.

    A burst of edits to one file queues it many times; indexing it once
    catches up with all of them, since the index reads the file as it is now.
    """
    try:
        fh = _pending_path(root).open("r+", encoding="utf-8")
    except FileNotFoundError:
        return []
    with fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        lines = fh.read().splitlines()
        fh.seek(0)
        fh.truncate()
    return [Path(line) for line in dict.fromkeys(lines) if line]


# ── Lock ──────────────────────────────────────────────────────────────────────


def _try_lock(lock: Path) -> bool:
    """Take ``lock``, clearing it first if its holder is gone.

    Two processes clearing the same stale lock at once can both come away
    holding it. That costs one round of the concurrent writes the lock exists
    to prevent, which the store's own conflict retries absorb, not a wedge.
    """
    try:
        lock.mkdir(parents=True)
        return True
    except FileExistsError:
        pass
    except OSError:
        return False
    if _lock_held(lock):
        return False
    _unlock(lock)
    try:
        lock.mkdir(parents=True)
        return True
    except OSError:
        return False  # another process cleared it and won


def _lock_held(lock: Path) -> bool:
    """Whether ``lock`` exists and its holder is still running."""
    try:
        pid = int((lock / _LOCK_PID_FILE).read_text())
    except FileNotFoundError:
        try:
            age = time.time() - lock.stat().st_mtime
        except FileNotFoundError:
            return False
        return age < _UNOWNED_LOCK_GRACE_SECONDS
    except (OSError, ValueError):
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, owned by someone else
    return True


def _write_lock_pid(lock: Path, pid: int) -> None:
    try:
        (lock / _LOCK_PID_FILE).write_text(str(pid))
    except OSError:
        pass


def _unlock(lock: Path) -> None:
    shutil.rmtree(lock, ignore_errors=True)
