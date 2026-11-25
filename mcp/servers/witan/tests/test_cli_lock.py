"""``witan lock run`` serializes commands on one host and never strands its lock.

Every test drives the real CLI in a subprocess, because what is under test is
process behaviour: an flock held across processes, signals, and what the
kernel releases when a holder dies. Tests wait on what the CLI does (a line on
stderr, a file the command writes), never on how long it takes to start: a
``witan`` import under a parallel suite can take many seconds.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from witan.cli.lock import LOCK_TIMEOUT_EXIT, lock_dir, lock_path
from witan_core import _filelock

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="signals and flock inheritance are POSIX"
)

WITAN = [sys.executable, "-m", "witan", "lock", "run"]
WAIT_SECONDS = 120
# Touches its first argument, then becomes `sleep 60`, so a signal sent to the
# command reaches sleep itself, which has no handler.
STARTED_THEN_SLEEP = ["sh", "-c", 'touch "$1"; exec sleep 60', "sh"]


def _lock_run(name: str, *command: str, timeout: float | None = None, **kwargs):
    options = [] if timeout is None else ["--timeout", str(timeout)]
    return subprocess.Popen(
        [*WITAN, name, *options, "--", *command],
        stdin=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        **kwargs,
    )


def _is_held(path: Path) -> bool:
    fd = _filelock.acquire(path, blocking=False)
    if fd is None:
        return True
    _filelock.release(fd)
    return False


def _wait_for(predicate, what: str) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out waiting until {what}")
        time.sleep(0.05)


def _start_holder(name: str, tmp_path: Path, **kwargs):
    started = tmp_path / f"started-{name}"
    run = _lock_run(name, *STARTED_THEN_SLEEP, str(started), **kwargs)
    _wait_for(started.exists, "the command starts under the lock")
    return run


def test_a_name_becomes_one_safe_file_name_per_name():
    url_name = "tests:https://github.com/mitodl/agent-kit"
    path = lock_path(url_name)
    assert path.parent == lock_dir()
    assert ":" not in path.name and "/" not in path.name
    # Different names that reduce to the same safe stem keep separate locks.
    assert lock_path("tests_https___github.com_mitodl_agent-kit") != path
    assert len(lock_path("x" * 1000).name) < 255


def test_two_concurrent_runs_of_one_name_serialize(tmp_path):
    log = tmp_path / "intervals"
    record = (
        "import sys, time; t = time.time(); time.sleep(2); "
        "open(sys.argv[1], 'a').write(f'{t} {time.time()}\\n')"
    )
    # Hold the lock until both runs are waiting on it, so they really contend
    # rather than happening to start one after the other.
    fd = _filelock.acquire(lock_path("tests:x"))
    try:
        runs = [
            _lock_run("tests:x", sys.executable, "-c", record, str(log))
            for _ in range(2)
        ]
        for run in runs:
            assert "waiting for 'tests:x'" in run.stderr.readline()
    finally:
        _filelock.release(fd)
    for run in runs:
        run.communicate(timeout=WAIT_SECONDS)

    assert [run.returncode for run in runs] == [0, 0]
    first, second = sorted(
        tuple(map(float, line.split())) for line in log.read_text().splitlines()
    )
    assert first[1] <= second[0]


def test_different_names_do_not_wait_for_each_other():
    fd = _filelock.acquire(lock_path("tests:held"))
    try:
        run = _lock_run("tests:free", "true", timeout=0)
        assert run.wait(timeout=WAIT_SECONDS) == 0
    finally:
        _filelock.release(fd)


def test_the_commands_exit_code_passes_through():
    run = _lock_run("tests:exit", sys.executable, "-c", "raise SystemExit(7)")
    assert run.wait(timeout=WAIT_SECONDS) == 7


def test_timeout_gives_up_without_running_the_command(tmp_path):
    marker = tmp_path / "ran"
    fd = _filelock.acquire(lock_path("tests:busy"))
    try:
        run = _lock_run("tests:busy", "touch", str(marker), timeout=0.3)
        _, stderr = run.communicate(timeout=WAIT_SECONDS)
    finally:
        _filelock.release(fd)
    assert run.returncode == LOCK_TIMEOUT_EXIT
    assert "timed out" in stderr
    assert not marker.exists()


def test_interrupting_a_wait_exits_quietly_without_running_the_command(tmp_path):
    marker = tmp_path / "ran"
    fd = _filelock.acquire(lock_path("tests:waiting"))
    try:
        run = _lock_run("tests:waiting", "touch", str(marker))
        assert "waiting for" in run.stderr.readline()
        run.send_signal(signal.SIGINT)
        _, stderr = run.communicate(timeout=WAIT_SECONDS)
    finally:
        _filelock.release(fd)
    assert run.returncode == 128 + signal.SIGINT
    assert "Traceback" not in stderr
    assert not marker.exists()


@pytest.mark.parametrize(
    ("signum", "exit_code"),
    [(signal.SIGTERM, 128 + signal.SIGTERM), (signal.SIGINT, 128 + signal.SIGINT)],
)
def test_a_signalled_holder_stops_its_command_and_frees_the_lock(
    signum, exit_code, tmp_path
):
    name = f"tests:signalled-{signum}"
    # A new session has no controlling terminal, as under a runner. Run from a
    # terminal, witan would take a SIGINT as Ctrl-C the command already got.
    run = _start_holder(name, tmp_path, start_new_session=True)

    run.send_signal(signum)

    # sleep has no handler, so it only ends this soon if the signal reached it.
    assert run.wait(timeout=WAIT_SECONDS) == exit_code
    assert not _is_held(lock_path(name))


def test_a_signal_ignored_at_start_stays_ignored_for_the_command(tmp_path):
    def ignore_hangup():
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    run = _start_holder(
        "tests:nohup", tmp_path, start_new_session=True, preexec_fn=ignore_hangup
    )
    try:
        # A hangup to the whole group, as closing the terminal under nohup.
        os.killpg(run.pid, signal.SIGHUP)
        # Still running: neither witan nor the command took the hangup.
        with pytest.raises(subprocess.TimeoutExpired):
            run.wait(timeout=5)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(run.pid, signal.SIGKILL)
        run.wait(timeout=WAIT_SECONDS)


def test_a_killed_holder_does_not_leave_the_lock_held(tmp_path):
    run = _start_holder("tests:killed", tmp_path, start_new_session=True)

    os.killpg(run.pid, signal.SIGKILL)
    run.wait(timeout=WAIT_SECONDS)

    path = lock_path("tests:killed")
    _wait_for(lambda: not _is_held(path), "the kernel releases the lock")


def test_the_lock_stays_with_a_command_that_outlives_a_killed_witan(tmp_path):
    run = _start_holder("tests:orphan", tmp_path, start_new_session=True)
    path = lock_path("tests:orphan")

    run.kill()
    run.wait(timeout=WAIT_SECONDS)
    try:
        # The orphaned command is still using the resource, so the next
        # worker must not get in yet.
        assert _is_held(path)
    finally:
        os.killpg(run.pid, signal.SIGKILL)
    _wait_for(lambda: not _is_held(path), "the kernel releases the lock")


def test_the_lock_stays_with_a_process_the_command_left_running(tmp_path):
    started = tmp_path / "started"
    path = lock_path("tests:left-running")
    # The command exits at once, leaving a background child that still holds
    # the inherited descriptor, as a test runner's surviving subprocess would.
    run = _lock_run(
        "tests:left-running",
        "sh",
        "-c",
        '(touch "$1"; exec sleep 60) & exit 0',
        "sh",
        str(started),
        start_new_session=True,
    )
    try:
        assert run.wait(timeout=WAIT_SECONDS) == 0
        _wait_for(started.exists, "the background child starts")
        assert _is_held(path)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(run.pid, signal.SIGKILL)
    _wait_for(lambda: not _is_held(path), "the kernel releases the lock")
