"""SessionStart and PostToolUse hook logic, as plain Python.

Ported from the former codegraph-session-init.sh / codegraph-reindex.sh bash
scripts so hook invocation is a portable CLI command everywhere the
`witan-code` binary installs, matching the bare `witan-code
inject-context`/`checkpoint` pattern this package's other two hooks already
use (and witan's own `witan inject-context`/`session-checkpoint`).

ONE WRITER PER CHECKOUT. Both hooks put their target on the checkout's queue
and leave the write to a single detached drainer that holds the checkout's
lock: SessionStart queues the project dir for a full index, PostToolUse queues
the edited file. The per-edit hook used to index in the foreground, one
process per Edit/Write, so an agent fanning out N subagents in one worktree
put N uncoordinated writers on one branch view. Against a deployed graph they
lost each other's optimistic-concurrency races until the retry budget ran out
(Sentry WITAN-12).

The lock is a kernel ``flock`` on a file, taken by the hook and handed to the
drainer as an inherited descriptor. The kernel drops it when the holder exits
however it exits, so a killed indexer cannot leave the checkout locked.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

from witan_core import popen_detached
from witan_core.observability import get_logger

from . import indexer
from . import repo as repo_module
from .context import _busy_path, _lock_path, _project_dir, _state_path

logger = get_logger("witan.code.hooks")


def session_init() -> None:
    """SessionStart: seed/refresh the whole repo's code graph in the background.

    Best-effort and non-blocking: skips non-git directories, never raises, and
    returns immediately. If an indexer is already running for this checkout
    the refresh waits in its queue rather than being dropped.
    """
    project_dir = _project_dir()
    root = repo_module.root(project_dir)
    if root is None:
        return
    _submit(root, project_dir)


def reindex_hook(payload: str) -> None:
    """PostToolUse (matcher ``Edit|Write``): reindex the edited file.

    ``payload`` is the raw hook JSON read from stdin. Inside a git checkout
    the path is queued for the checkout's drainer (see the module docstring
    for why), so the index lands a few seconds after the edit rather than
    before the hook returns. Outside one there is no branch view to contend
    over and the file is indexed in the foreground, as before.

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
    _submit(root, path)


def drain_pending(checkout: Path, lock_fd: int) -> None:
    """Index everything queued for ``checkout``, one target at a time.

    Run by the detached child :func:`_submit` spawns, holding the lock it was
    handed as ``lock_fd``. After letting go it looks at the queue once more
    and takes the lock back if anything arrived: a hook that queued while the
    lock was held did not spawn anyone, trusting this re-check to pick its
    target up.
    """
    busy = _busy_path(checkout)
    while True:
        try:
            busy.touch()
            for target in _take_pending(checkout):
                if not target.exists():
                    continue  # deleted or moved since it was queued
                try:
                    indexer.index_path(target, force=False)
                except Exception as exc:  # noqa: BLE001 — one target must not stop the rest
                    logger.warning(
                        "witan.code.hooks.reindex_failed",
                        path=str(target),
                        error=str(exc),
                    )
        finally:
            busy.unlink(missing_ok=True)
            os.close(lock_fd)
        if not _has_pending(checkout):
            return
        next_fd = _try_lock(checkout)
        if next_fd is None:
            return  # a newer drainer has it, and drains before letting go
        lock_fd = next_fd


def _submit(checkout: Path, target: Path) -> None:
    """Queue ``target`` and start a drainer unless one is already running.

    The enqueue comes FIRST, and that order is what makes the handoff safe: a
    drainer re-checks the queue after releasing the lock, so a target queued
    while it held the lock is picked up by that re-check, and one queued after
    the release finds the lock free here.
    """
    try:
        _enqueue(checkout, target)
        lock_fd = _try_lock(checkout)
    except OSError:
        return
    if lock_fd is None:
        return
    try:
        with _state_path(checkout, "log").open("w") as log:
            popen_detached(
                [
                    sys.executable,
                    "-m",
                    "witan_code",
                    "_drain-pending",
                    str(checkout),
                    str(lock_fd),
                ],
                pass_fds=(lock_fd,),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
    except OSError:
        pass  # the queue survives; the next hook to find the lock free drains it
    finally:
        # The child holds its own copy of the descriptor, and a flock belongs
        # to the open file, not the descriptor, so closing ours releases
        # nothing while the child runs.
        os.close(lock_fd)


# ── Pending queue ─────────────────────────────────────────────────────────────


def _enqueue(checkout: Path, target: Path) -> None:
    line = str(target)
    if "\n" in line:
        return  # would split into two bogus entries
    with _state_path(checkout, "pending").open("a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        fh.write(f"{line}\n")


def _has_pending(checkout: Path) -> bool:
    try:
        return _state_path(checkout, "pending").stat().st_size > 0
    except FileNotFoundError:
        return False


def _take_pending(checkout: Path) -> list[Path]:
    """Empty the queue and return its targets, each once, in first-queued order.

    A burst of edits to one file queues it many times; indexing it once
    catches up with all of them, since the index reads the file as it is now.
    """
    try:
        fh = _state_path(checkout, "pending").open("r+", encoding="utf-8")
    except FileNotFoundError:
        return []
    with fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        lines = fh.read().splitlines()
        fh.seek(0)
        fh.truncate()
    return [Path(line) for line in dict.fromkeys(lines) if line]


# ── Lock ──────────────────────────────────────────────────────────────────────


def _try_lock(checkout: Path) -> int | None:
    """Take the checkout's lock without waiting; its descriptor, or ``None``."""
    fd = os.open(_lock_path(checkout), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd
