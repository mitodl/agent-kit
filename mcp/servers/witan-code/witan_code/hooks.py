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

ONE WRITE PER FLUSH. The drainer indexes its whole queue as one batch, and
waits out ``FLUSH_INTERVAL_SECONDS`` since the checkout's last flush before
starting the next, so a session editing every few seconds writes its branch
view a bounded number of times per interval rather than once per edit.

The lock is a kernel file lock (``witan_core._filelock``: ``flock`` on POSIX,
``msvcrt.locking`` on Windows). The OS drops it when the holder exits however
it exits, so a killed indexer cannot leave the checkout locked. On POSIX the
hook takes it and hands it to the drainer as an inherited descriptor. Windows
has no way to hand a lock to a child (see :data:`_HANDS_OFF_LOCK`), so there
the hook only checks that the lock is free and the drainer takes its own.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from witan_core import _filelock, popen_detached
from witan_core.observability import get_logger

from . import indexer
from . import repo as repo_module
from .context import _busy_path, _lock_path, _project_dir, _state_path

logger = get_logger("witan.code.hooks")

_LOG_MAX_BYTES = 1024 * 1024

# Whether the hook passes its locked descriptor to the drainer, so the
# checkout is never unlocked between the two. Not on Windows: subprocess has
# no pass_fds there, and an msvcrt lock belongs to the process that took it,
# not to a handle a child inherits. There the hook releases its probe and the
# drainer takes the lock itself; a drainer that loses that race exits, and
# the holder that beat it drains its target, which was queued before the
# probe and so before the holder took the lock.
_HANDS_OFF_LOCK = sys.platform != "win32"

# The least time between two flushes of one checkout's queue, which bounds how
# often an edit session writes its branch view: at most one batched index per
# interval, and a batch under `indexer._DELETE_BATCH_SIZE` statements of
# deletes and `chunking.MCP_LOAD_MAX_BYTES` of records is three commits on a
# deployed graph's branch view (one delete, one load, one content-hash load;
# see `indexer._hashes_need_deferring`), plus the bridge view's own write.
# Before this an edit-heavy session indexed each edit as its own run, back
# to back, about three commits every 10-20s. The first edit
# after a quiet spell still flushes at once; the cost is that an edit landing
# inside the interval reaches the graph up to this much later.
FLUSH_INTERVAL_SECONDS = 30.0

# Codex edits files with its `apply_patch` tool, whose hook payload carries the
# patch text (`tool_input.command`) rather than a path; its `Edit|Write`
# matcher selects that tool by alias. These are the patch grammar's
# file-header lines (codex-rs/apply-patch parser.rs: ADD_FILE_MARKER,
# DELETE_FILE_MARKER, UPDATE_FILE_MARKER, MOVE_TO_MARKER). Every path they
# name is queued, including the ones the patch removed: a Delete File, and the
# Update File source of a Move to. The drainer indexes the ones still on disk
# and purges the rest (`indexer.index_path` on a vanished file). Codex accepts
# an indented Add/Delete/Update header (streaming_parser.rs trims the line
# outside an update hunk) but only a column-0 Move to. Added content starts
# with `+`, so it can't name a file; an update hunk's context line that quotes
# a header can, and at worst reindexes an unchanged file or purges one that is
# already gone.
_APPLY_PATCH_TARGET = re.compile(
    r"^(?:[ \t]*\*\*\* (?:Add File|Delete File|Update File)|\*\*\* Move to): (.+)$",
    re.MULTILINE,
)


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
    for why), so the index lands after the hook returns, up to
    ``FLUSH_INTERVAL_SECONDS`` later when an earlier edit was just flushed. Outside one there is no branch view to contend
    over and the file is indexed in the foreground, as before.

    Claude Code's payload names one file; Codex's ``apply_patch`` payload
    names every file in the patch, and each is reindexed. A named file that is
    no longer on disk (deleted, or the old side of a rename) has its rows
    purged instead.

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
    if data.get("tool_name") == "apply_patch":
        patch = tool_input.get("command")
        targets = (
            [m.strip() for m in _APPLY_PATCH_TARGET.findall(patch)]
            if isinstance(patch, str)
            else []
        )
    else:
        raw = (
            tool_input.get("file_path")
            or tool_input.get("path")
            or tool_input.get("filename")
        )
        targets = [raw] if isinstance(raw, str) and raw else []
    for raw in targets:
        _reindex_one(raw)


def _reindex_one(raw: str) -> None:
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    if path.exists():
        if not path.is_file():
            return
    elif not indexer.indexable(path):
        return  # never had rows, so nothing to purge

    root = repo_module.root(indexer.anchor_dir(path))
    if root is None:
        try:
            indexer.index_path(path, force=False)
        except Exception:  # noqa: BLE001, S110 — a parse failure must not fail the hook
            pass
        return
    _submit(root, path)


def drain_pending(checkout: Path, lock_fd: int | None) -> None:
    """Index everything queued for ``checkout`` as one batch per flush.

    Run by the detached child :func:`_submit` spawns, holding the lock it was
    handed as ``lock_fd``, or taking it itself when handed ``None`` (Windows,
    see :data:`_HANDS_OFF_LOCK`) and exiting if another drainer has it. A
    flush waits out :data:`FLUSH_INTERVAL_SECONDS` since the checkout's
    previous one, so edits that land in the meantime join it rather than each
    starting their own write. After letting go it
    looks at the queue once more and takes the lock back if anything arrived:
    a hook that queued while the lock was held did not spawn anyone, trusting
    this re-check to pick its target up.
    """
    if lock_fd is None:
        lock_fd = _try_lock(checkout)
        if lock_fd is None:
            return
    busy = _busy_path(checkout)
    while True:
        try:
            # Not busy while only waiting out the interval: the context block
            # reads this marker as "a reindex is running" and holds back its
            # broken-store warning on it.
            busy.unlink(missing_ok=True)
            _await_flush_slot(checkout)
            busy.touch()
            # One gone from disk is indexed too, which purges its rows,
            # unless the whole checkout went with it: git would then resolve
            # the path against whatever repo encloses it.
            targets = [
                target
                for target in _take_pending(checkout)
                if target.exists() or (checkout.is_dir() and indexer.indexable(target))
            ]
            if targets:
                _stamp_flush(checkout)
            for batch in _by_checkout(targets):
                if not indexer.cluster_serves(batch[0]):
                    continue
                try:
                    indexer.index_paths(batch, force=False)
                except Exception as exc:  # noqa: BLE001 — the drainer must still release
                    logger.warning(
                        "witan.code.hooks.reindex_failed",
                        paths=[str(target) for target in batch],
                        error=str(exc),
                    )
        finally:
            busy.unlink(missing_ok=True)
            _filelock.release(lock_fd)
        if not _has_pending(checkout):
            return
        next_fd = _try_lock(checkout)
        if next_fd is None:
            return  # a newer drainer has it, and drains before letting go
        lock_fd = next_fd


def _by_checkout(targets: list[Path]) -> list[list[Path]]:
    """Split ``targets`` into batches :func:`indexer.index_paths` accepts.

    A target is queued under the git root of its unresolved path, but
    ``index_paths`` groups by the resolved one, so a symlink into another
    repo (or a path since swallowed by a nested repo) would make it refuse
    the whole batch and drop every other edit with it. Targets outside any
    repo are indexed one at a time, since there is no checkout to share. So
    is one that cannot be resolved (before Python 3.13, resolve() raises
    RuntimeError on a symlink loop), which leaves its failure to that
    batch's own handler rather than to the whole flush.
    """
    groups: dict[Path, list[Path]] = {}
    loose: list[list[Path]] = []
    for target in targets:
        try:
            root = repo_module.root(indexer.anchor_dir(target.resolve()))
        except (OSError, RuntimeError):
            root = None
        if root is None:
            loose.append([target])
        else:
            groups.setdefault(root, []).append(target)
    return [*groups.values(), *loose]


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
    handoff: int | None = lock_fd if _HANDS_OFF_LOCK else None
    # Marked busy here rather than only once the drainer is up, so a prompt
    # that fires right after SessionStart already says indexing is under way.
    busy = _busy_path(checkout)
    log_path = _state_path(checkout, "log")
    try:
        if handoff is None:
            _filelock.release(lock_fd)
        busy.touch()
        # Appended, so a failure is still there after the next edit spawns a
        # new drainer; restarted once it grows past the cap.
        if log_path.exists() and log_path.stat().st_size > _LOG_MAX_BYTES:
            # Windows refuses while an exiting drainer still has it open as
            # stdout. A log past the cap must not cost the spawn.
            with contextlib.suppress(OSError):
                log_path.unlink()
        with log_path.open("a") as log:
            popen_detached(
                [
                    sys.executable,
                    "-m",
                    "witan_code",
                    "_drain-pending",
                    str(checkout),
                    *([] if handoff is None else [str(handoff)]),
                ],
                pass_fds=() if handoff is None else (handoff,),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
    except OSError:
        # The queue survives; the next hook to find the lock free drains it.
        busy.unlink(missing_ok=True)
    finally:
        # The child holds its own copy of the descriptor, and a flock belongs
        # to the open file, not the descriptor, so closing ours releases
        # nothing while the child runs. Closed, not released: unlocking would
        # unlock the child's copy too.
        if handoff is not None:
            os.close(handoff)


# ── Pending queue ─────────────────────────────────────────────────────────────


def _enqueue(checkout: Path, target: Path) -> None:
    line = str(target)
    if "\n" in line:
        return  # would split into two bogus entries
    with (
        _filelock.locked(_queue_lock_path(checkout)),
        _state_path(checkout, "pending").open("a", encoding="utf-8") as fh,
    ):
        fh.write(f"{line}\n")


def _queue_lock_path(checkout: Path) -> Path:
    """Guards the queue file. A file of its own rather than a lock on the queue
    itself, because an ``msvcrt`` lock is mandatory: Windows would refuse the
    holder's own reads and writes through any handle but the one locking it."""
    return _state_path(checkout, "pending.lock")


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
    with _filelock.locked(_queue_lock_path(checkout)):
        try:
            fh = _state_path(checkout, "pending").open("r+", encoding="utf-8")
        except FileNotFoundError:
            return []
        with fh:
            lines = fh.read().splitlines()
            fh.seek(0)
            fh.truncate()
    return [Path(line) for line in dict.fromkeys(lines) if line]


# ── Flush pacing ──────────────────────────────────────────────────────────────


def _flushed_path(checkout: Path) -> Path:
    """Stamped when a flush starts. A file, not drainer state, because a
    drainer exits once the queue is empty and the next edit spawns a new one,
    which must still wait out the previous drainer's interval."""
    return _state_path(checkout, "flushed")


def _stamp_flush(checkout: Path) -> None:
    # Set from time.time() rather than left to touch(), so the stamp and the
    # wait below read one clock.
    path = _flushed_path(checkout)
    path.touch()
    now = time.time()
    os.utime(path, (now, now))


def _await_flush_slot(checkout: Path) -> None:
    """Sleep until :data:`FLUSH_INTERVAL_SECONDS` have passed since the last flush.

    Capped at one interval, so a stamp from a clock that has since stepped
    back cannot stall the queue.
    """
    try:
        last = _flushed_path(checkout).stat().st_mtime
    except FileNotFoundError:
        return
    wait = min(last + FLUSH_INTERVAL_SECONDS - time.time(), FLUSH_INTERVAL_SECONDS)
    if wait > 0:
        time.sleep(wait)


# ── Lock ──────────────────────────────────────────────────────────────────────


def _try_lock(checkout: Path) -> int | None:
    """Take the checkout's lock without waiting; its descriptor, or ``None``.

    Release it with ``_filelock.release``. No ``O_NOFOLLOW`` guard: the lock
    file sits in :func:`~witan_code.context.state_dir`, which is private to
    this user, so nobody else can plant a symlink in its place.
    """
    return _filelock.acquire(_lock_path(checkout), blocking=False)
