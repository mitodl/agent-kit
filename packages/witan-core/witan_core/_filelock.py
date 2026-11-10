"""Cross-platform, cross-process exclusive file locks.

``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows, each imported only on
its own platform. ``fcntl`` is stdlib but POSIX-only, so a bare module-scope
``import fcntl`` in a base module makes every CLI fail at import on Windows.

POSIX keeps ``flock`` exactly, because other processes (an older client, the
``witan`` test that probes a store's lock from outside) take the same lock with
``flock`` directly, and a different primitive would not exclude them.

Both primitives lock per open file, not per process: a second ``open()`` of
the same lock file conflicts even in the thread that already holds it. Code
that nests (a merge holding the store lock around a load that takes it again,
``get_valid_token`` -> ``_refresh`` -> ``_store_token``) therefore needs the
re-entrant layer below, which counts depth per (thread, lock path). It is keyed
by thread, not process, so two threads doing unrelated writes still exclude
each other.
"""

from __future__ import annotations

import errno
import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["acquire", "acquire_reentrant", "locked", "release", "release_reentrant"]

if sys.platform == "win32":
    import msvcrt

    # msvcrt's own blocking mode (LK_LOCK) gives up after ten one-second
    # retries and raises, so a blocking acquire polls the non-blocking one.
    _POLL_SECONDS = 0.05

    def _try_lock(fd: int) -> bool:
        # msvcrt locks a byte range from the current position. One byte at
        # offset 0, past EOF of the empty lock file, which Windows allows.
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EDEADLOCK):
                return False
            raise
        return True

    def _lock(fd: int) -> None:
        while not _try_lock(fd):
            time.sleep(_POLL_SECONDS)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def acquire(path: Path, *, blocking: bool = True) -> int | None:
    """Open ``path`` and lock it exclusively. Not re-entrant.

    :param path: The lock file, created (with its parents) if missing.
    :param blocking: Wait for the lock. When false, return ``None`` instead of
        waiting if another holder has it.
    :returns: The locked descriptor, to hand to :func:`release`, or ``None``
        when ``blocking`` is false and the lock is held elsewhere.
    :rtype: int | None
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        if blocking:
            _lock(fd)
        elif not _try_lock(fd):
            os.close(fd)
            return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def release(fd: int) -> None:
    """Unlock and close a descriptor from :func:`acquire`.

    Unlocks before closing because Windows only guarantees a lock is freed
    promptly when it is released explicitly.
    """
    try:
        _unlock(fd)
    finally:
        os.close(fd)


_held: dict[tuple[int, str], int] = {}
_held_guard = threading.Lock()


def acquire_reentrant(path: Path) -> int | None:
    """Lock ``path`` for this thread, or note a re-entry if it already holds it.

    :returns: The descriptor to release, or ``None`` on a re-entry. Either way
        the caller must call :func:`release_reentrant` once.
    :rtype: int | None
    """
    key = (threading.get_ident(), str(path))
    with _held_guard:
        if _held.get(key):
            _held[key] += 1
            return None
    fd = acquire(path)
    with _held_guard:
        _held[key] = 1
    return fd


def release_reentrant(path: Path, fd: int | None) -> None:
    """Undo one :func:`acquire_reentrant`, unlocking at depth zero."""
    key = (threading.get_ident(), str(path))
    with _held_guard:
        depth = _held.get(key, 0)
        if depth > 1:
            _held[key] = depth - 1
            return
        _held.pop(key, None)
    if fd is not None:
        release(fd)


@contextmanager
def locked(path: Path) -> Iterator[None]:
    """Hold ``path`` exclusively for the block, re-entrant per thread."""
    fd = acquire_reentrant(path)
    try:
        yield
    finally:
        release_reentrant(path, fd)
