"""GroupCommitter: per-caller outcomes when separate callers share a commit.

Every test drives real threads through one committer with a fake ``commit``
whose first call blocks until released, so the writes submitted meanwhile are
known to be queued behind a commit in flight.
"""

from __future__ import annotations

import threading
import time

import pytest

from witan_core.group_commit import GroupCommitter, RejectedBatch
from witan_core.omnigraph import OmnigraphConflict

KEY = ("graph", "actor")


def _step(name: str) -> tuple[str, str, dict]:
    return ("mutations.gq", name, {"slug": name})


class FakeStore:
    """Records each commit and answers from a per-test ``decide``.

    ``decide(steps, if_commit)`` returns ``None`` for success or an exception
    to raise. The first commit blocks on ``release`` so a test can queue more
    writes behind it.
    """

    def __init__(self, decide=None) -> None:
        self.calls: list[tuple[list[str], str | None, bool]] = []
        self.release = threading.Event()
        self.in_flight = threading.Event()
        self.decide = decide or (lambda steps, if_commit: None)
        self._head = 0
        self._lock = threading.Lock()

    def commit(self, steps, if_commit, surface_conflict, started):
        with self._lock:
            first = not self.calls
            self.calls.append(([s[1] for s in steps], if_commit, surface_conflict))
        if first:
            self.in_flight.set()
            assert self.release.wait(5)
        error = self.decide(steps, if_commit)
        if error is not None:
            raise error
        with self._lock:
            parent, self._head = f"c{self._head}", self._head + 1
            return f"c{self._head}", parent


class Caller(threading.Thread):
    def __init__(self, committer, store, steps, **kwargs) -> None:
        super().__init__(daemon=True)
        self.args = (committer, store, steps, kwargs)
        self.result = None
        self.error: BaseException | None = None

    def run(self) -> None:
        committer, store, steps, kwargs = self.args
        try:
            self.result = committer.submit(KEY, steps, store.commit, **kwargs)
        except BaseException as exc:  # noqa: BLE001 — asserted on by the test
            self.error = exc


def _queue_behind_first(committer, store, first, others):
    """Start ``first``, wait until its commit is in flight, then queue ``others``."""
    first.start()
    assert store.in_flight.wait(5)
    for caller in others:
        caller.start()
    deadline = time.monotonic() + 5
    while sum(len(lane.queue) for lane in committer._lanes.values()) < len(others):
        assert time.monotonic() < deadline, "callers never queued"
        time.sleep(0.005)
    store.release.set()
    for caller in [first, *others]:
        caller.join(5)
        assert not caller.is_alive()


def test_a_lone_write_commits_at_once_and_alone():
    committer = GroupCommitter(OmnigraphConflict)
    store = FakeStore()
    store.release.set()

    assert committer.submit(KEY, [_step("a")], store.commit) == "c1"
    assert store.calls == [(["a"], None, False)]
    assert committer._lanes == {}, "an idle lane is dropped"


def test_writes_queued_behind_a_commit_share_the_next_one():
    committer = GroupCommitter(OmnigraphConflict)
    store = FakeStore()
    first = Caller(committer, store, [_step("a")])
    others = [Caller(committer, store, [_step(n)]) for n in "bcde"]

    _queue_behind_first(committer, store, first, others)

    assert committer.commits == [1, 4]
    assert store.calls[1] == (["b", "c", "d", "e"], None, False)
    assert first.result == "c1"
    assert {c.result for c in others} == {"c2"}
    assert all(c.error is None for c in [first, *others])


def test_one_fenced_write_per_commit_and_it_goes_first():
    committer = GroupCommitter(OmnigraphConflict)
    store = FakeStore()
    first = Caller(committer, store, [_step("a")])
    plain = Caller(committer, store, [_step("plain")])
    fenced = Caller(committer, store, [_step("fenced")], if_commit="c1")
    second_fenced = Caller(committer, store, [_step("fenced2")], if_commit="c2")

    _queue_behind_first(committer, store, first, [plain, fenced, second_fenced])

    assert store.calls[1] == (["fenced", "plain"], "c1", False)
    assert store.calls[2] == (["fenced2"], "c2", False)
    assert [c.error for c in (plain, fenced, second_fenced)] == [None] * 3


def test_a_refused_fence_fails_only_its_own_caller():
    """The riders are not told anything happened to them: they commit in the
    next commit, and the fenced caller gets the conflict to re-read on."""

    def decide(steps, if_commit):
        if if_commit == "stale":
            return OmnigraphConflict("precondition failed")
        return None

    committer = GroupCommitter(OmnigraphConflict)
    store = FakeStore(decide)
    first = Caller(committer, store, [_step("a")])
    fenced = Caller(committer, store, [_step("fenced")], if_commit="stale")
    riders = [Caller(committer, store, [_step(n)]) for n in ("r1", "r2")]

    _queue_behind_first(committer, store, first, [fenced, *riders])

    assert isinstance(fenced.error, OmnigraphConflict)
    assert all(r.error is None for r in riders)
    assert store.calls[1][0] == ["fenced", "r1", "r2"]
    assert store.calls[2] == (["r1", "r2"], None, False)
    assert {r.result for r in riders} == {"c2"}


def test_a_fence_this_lane_already_moved_past_is_refused_without_a_commit():
    committer = GroupCommitter(OmnigraphConflict)
    store = FakeStore()
    first = Caller(committer, store, [_step("a")])
    # The first commit replaces head c0, so a write fenced on c0 cannot land.
    stale = Caller(committer, store, [_step("stale")], if_commit="c0")
    rider = Caller(committer, store, [_step("rider")])

    _queue_behind_first(committer, store, first, [stale, rider])

    assert isinstance(stale.error, OmnigraphConflict)
    assert store.calls[1] == (["rider"], None, False)
    assert committer.commits == [1, 1]


def test_a_rejected_group_is_replayed_one_write_at_a_time():
    def decide(steps, if_commit):
        if any(s[1] == "bad" for s in steps):
            return RejectedBatch(RuntimeError("omnigraph mutate failed: 400"))
        return None

    committer = GroupCommitter(OmnigraphConflict)
    store = FakeStore(decide)
    first = Caller(committer, store, [_step("a")])
    good = Caller(committer, store, [_step("good")])
    bad = Caller(committer, store, [_step("bad")])
    good2 = Caller(committer, store, [_step("good2")])

    _queue_behind_first(committer, store, first, [good, bad, good2])

    assert str(bad.error) == "omnigraph mutate failed: 400"
    assert type(bad.error) is RuntimeError
    assert good.error is None and good2.error is None
    assert [c[0] for c in store.calls[1:]] == [
        ["good", "bad", "good2"],
        ["good"],
        ["bad"],
        ["good2"],
    ]


def test_an_outcome_unknown_failure_reaches_every_member_and_is_not_replayed():
    """A lost response may have committed, so replaying would write it twice."""

    def decide(steps, if_commit):
        if len(steps) > 1:
            return RuntimeError("request failed mid-flight")
        return None

    committer = GroupCommitter(OmnigraphConflict)
    store = FakeStore(decide)
    first = Caller(committer, store, [_step("a")])
    others = [Caller(committer, store, [_step(n)]) for n in ("b", "c")]

    _queue_behind_first(committer, store, first, others)

    assert len(store.calls) == 2
    for caller in others:
        assert str(caller.error) == "request failed mid-flight"
    assert others[0].error is not others[1].error, "each caller raises its own"


def test_deletes_never_share_a_commit_with_inserts():
    committer = GroupCommitter(OmnigraphConflict)
    store = FakeStore()
    first = Caller(committer, store, [_step("a")])
    insert1 = Caller(committer, store, [_step("i1")])
    delete = Caller(committer, store, [_step("d")], destructive=True)
    insert2 = Caller(committer, store, [_step("i2")])

    _queue_behind_first(committer, store, first, [insert1, delete, insert2])

    assert [c[0] for c in store.calls[1:]] == [["i1", "i2"], ["d"]]


def test_the_statement_cap_splits_a_queue():
    committer = GroupCommitter(OmnigraphConflict, max_statements=3)
    store = FakeStore()
    first = Caller(committer, store, [_step("a")])
    others = [
        Caller(committer, store, [_step(f"s{i}"), _step(f"t{i}")]) for i in range(3)
    ]

    _queue_behind_first(committer, store, first, others)

    assert committer.commits == [1, 1, 1, 1]
    oversized = Caller(
        committer, store, [_step("x"), _step("y"), _step("z"), _step("w")]
    )
    oversized.run()
    assert oversized.error is None, "a write over the cap still commits, alone"


def test_lanes_are_independent():
    """A commit in flight on one actor's lane does not hold up another's."""
    committer = GroupCommitter(OmnigraphConflict)
    blocked = FakeStore()
    other = FakeStore()
    other.release.set()
    stuck = threading.Thread(
        target=committer.submit, args=(("g", "A"), [_step("a")], blocked.commit)
    )
    stuck.start()
    assert blocked.in_flight.wait(5)

    assert committer.submit(("g", "B"), [_step("b")], other.commit) == "c1"

    blocked.release.set()
    stuck.join(5)


@pytest.mark.parametrize("writers", [8, 32])
def test_every_caller_gets_exactly_its_own_outcome_under_load(writers):
    """Random-ish interleavings: odd writers are rejected, the rest commit, and
    nobody's outcome is swapped or lost."""

    def decide(steps, if_commit):
        if any(int(s[2]["slug"]) % 2 for s in steps):
            return RejectedBatch(RuntimeError("rejected"))
        return None

    committer = GroupCommitter(OmnigraphConflict)
    store = FakeStore(decide)
    store.release.set()
    callers = [
        Caller(committer, store, [("mutations.gq", "w", {"slug": str(i)})])
        for i in range(writers)
    ]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(10)

    for i, caller in enumerate(callers):
        assert (caller.error is None) == (i % 2 == 0), (i, caller.error)
    assert committer._lanes == {}
