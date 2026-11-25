"""``witan lock run`` — a host-local named lock around a command.

Parallel workers on one machine contend for resources that only exist on that
machine (a repo's Docker test stack, a fixed port). The contention never
crosses hosts, so the lock is an exclusive file lock under the local state dir
rather than anything in the graph.

The lock is the open file description: the kernel drops it when the last
descriptor closes, so a holder that dies by any means, ``SIGKILL`` included,
cannot leave it held. The command inherits the descriptor (as ``flock(1)``
does by default), so killing only ``witan`` leaves the lock with a command
that is still using the resource instead of letting the next worker in.
"""

from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated

import cyclopts

from agent_config_kit.config import xdg_base_dir
from witan_core import _filelock

from ._common import app, print_error

lock_app = cyclopts.App(
    name="lock",
    help="Host-local named locks for commands that parallel workers contend on.",
)
app.command(lock_app, name="lock")

# sysexits.h EX_TEMPFAIL: the lock was busy, so a later retry can succeed. Kept
# apart from the command's own codes so a runner can tell "never ran" from
# "ran and failed".
LOCK_TIMEOUT_EXIT = 75
# Shell conventions for a command that could not be started.
COMMAND_NOT_EXECUTABLE_EXIT = 126
COMMAND_NOT_FOUND_EXIT = 127
# A command killed by signal N exits 128 + N, as a shell reports it.
SIGNAL_EXIT_BASE = 128
LOCK_POLL_SECONDS = 0.1
# Room for the digest and suffix inside a 255-byte file name.
_NAME_STEM_CHARS = 100
_NAME_DIGEST_CHARS = 16
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def lock_dir() -> Path:
    """Where named locks live: ``${XDG_STATE_HOME:-~/.local/state}/witan/locks``."""
    return xdg_base_dir("XDG_STATE_HOME", Path(".local") / "state") / "witan" / "locks"


def lock_path(name: str) -> Path:
    """The lock file for ``name``.

    Names carry ``:`` and ``/`` (``tests:https://github.com/org/repo``), so
    they are reduced to a safe stem for readability, and a digest of the full
    name keeps two names that reduce to the same stem from sharing a lock.
    """
    stem = _UNSAFE_NAME_CHARS.sub("_", name)[:_NAME_STEM_CHARS]
    digest = hashlib.sha256(name.encode()).hexdigest()[:_NAME_DIGEST_CHARS]
    return lock_dir() / f"{stem}-{digest}.lock"


def _acquire(path: Path, name: str, timeout: float | None) -> int | None:
    fd = _filelock.acquire(path, blocking=False)
    if fd is not None:
        return fd
    print(f"witan lock: waiting for {name!r}", file=sys.stderr, flush=True)
    if timeout is None:
        return _filelock.acquire(path)
    deadline = time.monotonic() + timeout
    while (fd := _filelock.acquire(path, blocking=False)) is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        # Capped so the last attempt lands at the deadline, not up to a poll
        # interval past it.
        time.sleep(min(LOCK_POLL_SECONDS, remaining))
    return fd


def _terminal_delivered_sigint() -> bool:
    """Whether a SIGINT we got already reached the command from the terminal.

    Ctrl-C signals the terminal's whole foreground process group, which the
    command shares with us. Forwarding it again would hand the command a
    second interrupt, which cuts short the cleanup the first one started (a
    test run tearing down its containers). Any other SIGINT (``kill -INT`` from
    a runner) reached only us, so it is forwarded. The controlling terminal is
    opened directly because stdin may be a pipe while Ctrl-C still reaches the
    group. On Windows Ctrl-C goes to every process on the console, and
    ``Popen.send_signal`` cannot send it.
    """
    if sys.platform == "win32":
        return True
    try:
        tty = os.open("/dev/tty", os.O_RDONLY)
    except OSError:
        return False
    try:
        return os.tcgetpgrp(tty) == os.getpgrp()
    except OSError:
        return False
    finally:
        os.close(tty)


def _run_holding(fd: int, command: tuple[str, ...]) -> int:
    proc: subprocess.Popen | None = None
    pending: list[int] = []

    def forward(signum: int, _frame) -> None:
        if signum == signal.SIGINT and _terminal_delivered_sigint():
            return
        if proc is None:
            pending.append(signum)
        else:
            proc.send_signal(signum)

    forwarded = [signal.SIGINT, signal.SIGTERM]
    if sys.platform != "win32":
        forwarded.append(signal.SIGHUP)
    # A signal we were started ignoring (``nohup``, ``trap '' HUP``) stays
    # ignored, so the command inherits it ignored too, as it would without us.
    previous = {
        signum: signal.signal(signum, forward)
        for signum in forwarded
        if signal.getsignal(signum) is not signal.SIG_IGN
    }
    try:
        try:
            proc = subprocess.Popen(
                command, pass_fds=() if sys.platform == "win32" else (fd,)
            )
        except FileNotFoundError:
            print_error(f"witan lock: command not found: {command[0]}")
            return COMMAND_NOT_FOUND_EXIT
        except PermissionError:
            print_error(f"witan lock: command not executable: {command[0]}")
            return COMMAND_NOT_EXECUTABLE_EXIT
        for signum in pending:
            proc.send_signal(signum)
        # Keep waiting after forwarding a signal: the lock covers the
        # command's own shutdown, not only its normal run.
        returncode = proc.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return SIGNAL_EXIT_BASE - returncode if returncode < 0 else returncode


@lock_app.command(name="run")
def lock_run(
    name: str,
    *command: Annotated[str, cyclopts.Parameter(allow_leading_hyphen=True)],
    timeout: float | None = None,
) -> None:
    """Run a command while holding the host-local lock ``name``.

    Commands holding the same name on this machine run one at a time; other
    hosts are unaffected. Exits with the command's exit code (128 + N if a
    signal killed it). SIGINT, SIGTERM and SIGHUP are forwarded to the command,
    and the lock is held until it exits. Put witan's own options before ``--``,
    e.g. ``witan lock run tests:<repo> --timeout 600 -- just test``.

    Parameters
    ----------
    name:
        What the lock guards, e.g. ``tests:<repo-url>``. Any string.
    command:
        The command and its arguments, after ``--``.
    timeout:
        Seconds to wait for the lock before giving up with exit code 75
        without running the command. Waits indefinitely when omitted.
    """
    if not command:
        print_error("witan lock run: no command given; put it after `--`.")
        raise SystemExit(2)
    if timeout is not None and timeout < 0:
        print_error("witan lock run: --timeout must not be negative.")
        raise SystemExit(2)
    # A Ctrl-C before the command starts (while waiting, or before the
    # forwarding handlers are installed) ends witan quietly, as the
    # interrupted command would have.
    try:
        fd = _acquire(lock_path(name), name, timeout)
        if fd is None:
            print_error(f"witan lock: timed out after {timeout}s waiting for {name!r}")
            raise SystemExit(LOCK_TIMEOUT_EXIT)
        try:
            code = _run_holding(fd, command)
        finally:
            # An explicit unlock on POSIX would free the lock under anything
            # the command left running with the inherited descriptor, so only
            # witan's copy is closed and the kernel frees it after the last.
            if sys.platform == "win32":
                _filelock.release(fd)
            else:
                os.close(fd)
    except KeyboardInterrupt:
        raise SystemExit(SIGNAL_EXIT_BASE + signal.SIGINT) from None
    raise SystemExit(code)
