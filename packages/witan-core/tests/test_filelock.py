"""The cross-platform file lock (witan_core._filelock).

Written against the public behaviour only, so the same assertions hold for
``fcntl.flock`` on POSIX and ``msvcrt.locking`` on Windows.
"""

from __future__ import annotations

import subprocess
import sys

from witan_core import _filelock

_PROBE = """
import sys
from pathlib import Path
from witan_core import _filelock
fd = _filelock.acquire(Path(sys.argv[1]), blocking=False)
print("held" if fd is None else "free")
if fd is not None:
    _filelock.release(fd)
"""


def _probe(path) -> str:
    """Whether another process can take the lock right now."""
    out = subprocess.run(
        [sys.executable, "-c", _PROBE, str(path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return out.stdout.strip()


def test_lock_excludes_another_process(tmp_path):
    path = tmp_path / "sub" / "x.lock"
    fd = _filelock.acquire(path)
    try:
        assert _probe(path) == "held"
    finally:
        _filelock.release(fd)
    assert _probe(path) == "free"


def test_second_descriptor_in_one_process_conflicts(tmp_path):
    """Why the re-entrant layer exists: the lock is per open file, so the
    same process opening it again is refused like any other holder."""
    path = tmp_path / "x.lock"
    fd = _filelock.acquire(path)
    try:
        assert _filelock.acquire(path, blocking=False) is None
    finally:
        _filelock.release(fd)
    again = _filelock.acquire(path, blocking=False)
    assert again is not None
    _filelock.release(again)


def test_locked_is_reentrant_and_releases_at_depth_zero(tmp_path):
    path = tmp_path / "x.lock"
    with _filelock.locked(path), _filelock.locked(path):
        assert _probe(path) == "held"
    assert _probe(path) == "free"
    assert not _filelock._held
