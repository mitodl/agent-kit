"""`_launch` must hand the payload over on stdin AND drop the handle.

The bug this guards was not a crash in an edge case — it made the probe
unrunnable in its first phase on Python 3.11 and 3.12, before a single
measurement was taken. `_launch` writes each worker's payload to stdin and
closes it at launch (both halves deliberate: the payload carries a pinned
bearer token, so it must not go in argv where `ps` exposes it for the whole
lead interval; and deferring the write to collection time would serialise the
warmups and destroy the simultaneity the probe exists to create). But
`Popen.communicate()` opens by flushing stdin whenever `self.stdin` is
truthy — and a *closed* file object is still truthy — so the flush raised
`ValueError`, which `_communicate` does not catch because it guards only
`BrokenPipeError`.

WHY THIS FILE EXISTS RATHER THAN A COMMENT. Deleting `proc.stdin = None`
leaves the rest of the suite green: nothing else exercises `_launch`'s
`Popen` stdin lifecycle, and the real failure only appears on an interpreter
CI does not happen to run the probe on.

The fake below reproduces CPython's actual ordering rather than asserting the
assignment happened, so the test fails for the real reason on every supported
interpreter — including 3.13 and 3.14, which tolerate the live bug. Modelled
on the 3.12 stdlib: `communicate()` delegates to `_communicate`, which opens
`if self.stdin and not self._communication_started:` with a `self.stdin.flush()`
guarded only against `BrokenPipeError`.
"""

import json

import pytest

from witan.scripts import concurrency_probe


class _ClosableStdin:
    """A stdin handle with the one property that matters: closed but truthy."""

    def __init__(self):
        self.written = []
        self.closed = False

    def write(self, data):
        if self.closed:
            raise ValueError("I/O operation on closed file.")
        self.written.append(data)

    def close(self):
        self.closed = True

    def flush(self):
        # The exact stdlib behaviour, and the crux: a closed file raises here,
        # and it is a ValueError -- not the BrokenPipeError `_communicate`
        # guards -- so it escapes and aborts the phase.
        if self.closed:
            raise ValueError("I/O operation on closed file.")


class _FakePopen:
    """Enough of `Popen` to reproduce the 3.11/3.12 failure faithfully."""

    instances: list["_FakePopen"] = []

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.stdin = _ClosableStdin()
        # Kept separately so the payload stays inspectable after `_launch`
        # drops `self.stdin` -- which is the whole point of the fix.
        self.original_stdin = self.stdin
        self._stdin_at_communicate = "not-called"
        self.killed = False
        _FakePopen.instances.append(self)

    def communicate(self, timeout=None):
        self._stdin_at_communicate = self.stdin
        # Verbatim shape of CPython's `_communicate` preamble.
        if self.stdin:
            self.stdin.flush()
        row = {"index": 0, "mode": "claim", "ok": True}
        return json.dumps(row) + "\n", ""

    def kill(self):
        self.killed = True


@pytest.fixture
def fake_popen(monkeypatch):
    _FakePopen.instances = []
    monkeypatch.setattr(concurrency_probe.subprocess, "Popen", _FakePopen)
    # `_launch`'s deadline is absolute (start_at + WORKER_TIMEOUT_S); a
    # start_at in the past would hand communicate() a zero budget.
    monkeypatch.setattr(concurrency_probe.time, "time", lambda: 0.0)
    return _FakePopen


def test_launch_collects_a_row_rather_than_raising_on_a_closed_stdin(fake_popen):
    """The regression itself: this raised ValueError on 3.11 and 3.12."""
    rows = _launch_one()

    assert [r["ok"] for r in rows] == [True]


def test_launch_clears_the_stdin_handle_before_collecting(fake_popen):
    """Cleared, not merely closed -- closing alone is what broke it.

    Asserted at the moment `communicate()` runs rather than afterwards: a
    handle dropped after collection would satisfy an end-state check while
    still raising during it.
    """
    _launch_one()
    [proc] = fake_popen.instances

    assert proc._stdin_at_communicate is None


def test_launch_delivers_the_payload_before_dropping_the_handle(fake_popen):
    """Dropping the handle must not cost the payload the worker is waiting on.

    The token cannot go in argv, so if this write were skipped or reordered
    after the clear, every worker would block on an empty stdin and the phase
    would time out rather than fail loudly.
    """
    payload = {"token": "pinned", "graph": "council"}
    concurrency_probe._launch([("claim", 0, payload)], start_at=0.0)
    [proc] = fake_popen.instances

    assert json.loads("".join(proc.original_stdin.written)) == payload
    assert proc.original_stdin.closed
    # And the token stayed out of argv, which is why it goes on stdin at all.
    assert not any("pinned" in str(a) for a in proc.argv)


def _launch_one():
    return concurrency_probe._launch([("claim", 0, {"token": "pinned"})], start_at=0.0)
