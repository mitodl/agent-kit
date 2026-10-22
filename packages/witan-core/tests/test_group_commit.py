"""GroupCommitter: per-caller outcomes when separate callers share a commit.

Every test drives real threads through one committer with a fake ``commit``
whose first call blocks until released, so the writes submitted meanwhile are
known to be queued behind a commit in flight.
"""

from __future__ import annotations

import threading
import time

import pytest

from witan_core.group_commit import ConditionFailed, GroupCommitter, RejectedBatch
from witan_core.omnigraph import OmnigraphConflict, WriteQueueFull

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


def _queued(committer) -> int:
    return sum(len(lane.queue) for lane in committer._lanes.values())


def _queue_in_order(committer, callers):
    """Start ``callers`` one at a time, each queued before the next starts, so
    tests that assert on arrival order are deterministic."""
    for caller in callers:
        expected = _queued(committer) + 1
        caller.start()
        deadline = time.monotonic() + 5
        while _queued(committer) < expected:
            assert time.monotonic() < deadline, "caller never queued"
            time.sleep(0.005)


def _queue_behind_first(committer, store, first, others):
    """Start ``first``, wait until its commit is in flight, then queue ``others``."""
    first.start()
    assert store.in_flight.wait(5)
    _queue_in_order(committer, others)
    store.release.set()
    for caller in [first, *others]:
        caller.join(5)
        assert not caller.is_alive()


def test_a_lone_write_commits_at_once_and_alone():
    committer = GroupCommitter(OmnigraphConflict, WriteQueueFull)
    store = FakeStore()
    store.release.set()

    assert committer.submit(KEY, [_step("a")], store.commit) == "c1"
    assert store.calls == [(["a"], None, False)]
    assert committer._lanes == {}, "an idle lane is dropped"


def test_writes_queued_behind_a_commit_share_the_next_one():
    committer = GroupCommitter(OmnigraphConflict, WriteQueueFull)
    store = FakeStore()
    first = Caller(committer, store, [_step("a")])
    others = [Caller(committer, store, [_step(n)]) for n in "bcde"]

    _queue_behind_first(committer, store, first, others)

    assert list(committer.commits) == [1, 4]
    assert store.calls[1] == (["b", "c", "d", "e"], None, False)
    assert first.result == "c1"
    assert {c.result for c in others} == {"c2"}
    assert all(c.error is None for c in [first, *others])


def test_one_fenced_write_per_commit_and_it_goes_first():
    committer = GroupCommitter(OmnigraphConflict, WriteQueueFull)
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
            return ConditionFailed(OmnigraphConflict("precondition failed"))
        return None

    committer = GroupCommitter(OmnigraphConflict, WriteQueueFull)
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


@pytest.mark.parametrize("surface_conflict", [True, False])
def test_a_fence_this_lane_already_moved_past_is_refused_without_a_commit(
    surface_conflict,
):
    """Refused the way the server would have refused it: a conflict for a
    caller that surfaces them, the plain precondition error otherwise."""
    committer = GroupCommitter(OmnigraphConflict, WriteQueueFull)
    store = FakeStore()
    first = Caller(committer, store, [_step("a")])
    # The first commit replaces head c0, so a write fenced on c0 cannot land.
    stale = Caller(
        committer,
        store,
        [_step("stale")],
        if_commit="c0",
        surface_conflict=surface_conflict,
    )
    rider = Caller(committer, store, [_step("rider")])

    _queue_behind_first(committer, store, first, [stale, rider])

    expected = OmnigraphConflict if surface_conflict else RuntimeError
    assert type(stale.error) is expected
    assert "NOTHING WAS WRITTEN" in str(stale.error)
    assert store.calls[1] == (["rider"], None, False)
    assert list(committer.commits) == [1, 1]


def test_a_refused_fence_that_does_not_surface_conflicts_spares_the_riders():
    """A 412 for a caller without ``surface_conflict`` is a plain RuntimeError,
    but it is still that caller's alone."""
    refusal = RuntimeError("precondition no longer held. NOTHING WAS WRITTEN")

    def decide(steps, if_commit):
        return ConditionFailed(refusal) if if_commit == "stale" else None

    committer = GroupCommitter(OmnigraphConflict, WriteQueueFull)
    store = FakeStore(decide)
    first = Caller(committer, store, [_step("a")])
    fenced = Caller(committer, store, [_step("fenced")], if_commit="stale")
    rider = Caller(committer, store, [_step("rider")])

    _queue_behind_first(committer, store, first, [fenced, rider])

    assert type(fenced.error) is RuntimeError
    assert str(fenced.error) == str(refusal)
    assert rider.error is None and rider.result == "c2"


def test_a_write_whose_budget_runs_out_in_the_queue_is_never_sent():
    committer = GroupCommitter(OmnigraphConflict, WriteQueueFull)
    store = FakeStore()
    first = Caller(committer, store, [_step("a")])
    late = Caller(committer, store, [_step("late")], budget=0.05)
    patient = Caller(committer, store, [_step("patient")])
    first.start()
    assert store.in_flight.wait(5)
    late.start()
    patient.start()
    time.sleep(0.2)
    store.release.set()
    for caller in (first, late, patient):
        caller.join(5)

    assert isinstance(late.error, WriteQueueFull)
    assert "NOTHING WAS WRITTEN" in str(late.error)
    assert patient.error is None
    assert [c[0] for c in store.calls] == [["a"], ["patient"]]


def test_a_queued_caller_is_refused_at_its_own_deadline_not_the_leaders():
    """The commit ahead stays in flight past the follower's budget, and the
    follower still gets its refusal on time."""
    committer = GroupCommitter(OmnigraphConflict, WriteQueueFull)
    store = FakeStore()
    first = Caller(committer, store, [_step("a")])
    late = Caller(committer, store, [_step("late")], budget=0.05)
    first.start()
    assert store.in_flight.wait(5)
    late.start()

    late.join(2)
    assert not late.is_alive(), "the follower waited for the leader's commit"
    assert first.is_alive(), "the leader's commit is still in flight"
    assert isinstance(late.error, WriteQueueFull)
    assert "NOTHING WAS WRITTEN" in str(late.error)

    store.release.set()
    first.join(5)
    assert [c[0] for c in store.calls] == [["a"]]
    assert committer._lanes == {}


def test_a_replay_does_not_send_a_member_whose_budget_ran_out_meanwhile():
    """The rejected group and the replays ahead of a member can use up its
    budget; it is then refused, not sent."""

    def decide(steps, if_commit):
        names = [s[1] for s in steps]
        if "bad" in names:
            return RejectedBatch(RuntimeError("omnigraph mutate failed: 400"))
        if names == ["slow"]:
            time.sleep(0.6)
        return None

    committer = GroupCommitter(OmnigraphConflict, WriteQueueFull)
    store = FakeStore(decide)
    first = Caller(committer, store, [_step("a")])
    slow = Caller(committer, store, [_step("slow")])
    bad = Caller(committer, store, [_step("bad")])
    short = Caller(committer, store, [_step("short")], budget=0.5)

    _queue_behind_first(committer, store, first, [slow, bad, short])

    assert slow.error is None
    assert type(bad.error) is RuntimeError
    assert isinstance(short.error, WriteQueueFull)
    assert "NOTHING WAS WRITTEN" in str(short.error)
    assert [c[0] for c in store.calls[1:]] == [
        ["slow", "bad", "short"],
        ["slow"],
        ["bad"],
    ]


def test_a_rejected_group_is_replayed_one_write_at_a_time():
    def decide(steps, if_commit):
        if any(s[1] == "bad" for s in steps):
            return RejectedBatch(RuntimeError("omnigraph mutate failed: 400"))
        return None

    committer = GroupCommitter(OmnigraphConflict, WriteQueueFull)
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

    committer = GroupCommitter(OmnigraphConflict, WriteQueueFull)
    store = FakeStore(decide)
    first = Caller(committer, store, [_step("a")])
    others = [Caller(committer, store, [_step(n)]) for n in ("b", "c")]

    _queue_behind_first(committer, store, first, others)

    assert len(store.calls) == 2
    for caller in others:
        assert str(caller.error) == "request failed mid-flight"
    assert others[0].error is not others[1].error, "each caller raises its own"


def test_deletes_never_share_a_commit_with_inserts():
    committer = GroupCommitter(OmnigraphConflict, WriteQueueFull)
    store = FakeStore()
    first = Caller(committer, store, [_step("a")])
    insert1 = Caller(committer, store, [_step("i1")])
    delete = Caller(committer, store, [_step("d")], destructive=True)
    insert2 = Caller(committer, store, [_step("i2")])

    _queue_behind_first(committer, store, first, [insert1, delete, insert2])

    assert [c[0] for c in store.calls[1:]] == [["i1", "i2"], ["d"]]


def test_the_statement_cap_splits_a_queue():
    committer = GroupCommitter(OmnigraphConflict, WriteQueueFull, max_statements=3)
    store = FakeStore()
    first = Caller(committer, store, [_step("a")])
    others = [
        Caller(committer, store, [_step(f"s{i}"), _step(f"t{i}")]) for i in range(3)
    ]

    _queue_behind_first(committer, store, first, others)

    assert list(committer.commits) == [1, 1, 1, 1]
    oversized = Caller(
        committer, store, [_step("x"), _step("y"), _step("z"), _step("w")]
    )
    oversized.run()
    assert oversized.error is None, "a write over the cap still commits, alone"


def test_lanes_are_independent():
    """A commit in flight on one actor's lane does not hold up another's."""
    committer = GroupCommitter(OmnigraphConflict, WriteQueueFull)
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

    committer = GroupCommitter(OmnigraphConflict, WriteQueueFull)
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


# ── How the client classifies a failed group commit ──────────────────────────


@pytest.fixture
def remote(monkeypatch, tmp_path):
    """A remote client whose HTTP call is a stub returning one attempt."""
    import shutil

    from witan_core import omnigraph as og

    (tmp_path / "m.gq").write_text(
        "query put($slug: String) {\n    insert T { slug: $slug }\n}\n"
    )
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    client = og.OmnigraphClient("http://og.test", tmp_path, graph_id="g")

    def answer(status=None, error="", raises=None):
        def stub(*args, attempts=None, **kwargs):
            attempts.append(og._AttemptResult("fatal", error=error, status=status))
            raise raises or RuntimeError(f"omnigraph mutate failed:\n{error}")

        monkeypatch.setattr(client, "_http_execute", stub)

    return client, answer


STEPS = [("m.gq", "put", {"slug": "a"}), ("m.gq", "put", {"slug": "b"})]


def test_a_validation_400_is_a_rejection_safe_to_replay(remote):
    client, answer = remote
    answer(400, "__src 'x' not found in Task (HTTP 400, bad_request)")
    with pytest.raises(RejectedBatch):
        client._commit_group(STEPS, None, False, time.monotonic())


def test_a_datafusion_400_is_not_replayed(remote):
    """omnigraph marks a DataFusion error completion-uncertain server-side
    while still answering 400, so a replay could apply a write twice."""
    client, answer = remote
    answer(400, "query: execution failed (HTTP 400, bad_request)")
    with pytest.raises(RuntimeError) as raised:
        client._commit_group(STEPS, None, False, time.monotonic())
    assert type(raised.value) is RuntimeError


def test_a_412_belongs_to_the_fenced_member(remote):
    client, answer = remote
    answer(412, "precondition failed on branch 'main' (HTTP 412)")
    with pytest.raises(ConditionFailed) as raised:
        client._commit_group(STEPS, "c0", False, time.monotonic())
    assert type(raised.value.error) is RuntimeError


def test_a_surfaced_conflict_belongs_to_the_conditional_member(remote):
    client, answer = remote
    answer(409, "stale view", raises=OmnigraphConflict("stale view"))
    with pytest.raises(ConditionFailed) as raised:
        client._commit_group(STEPS, None, True, time.monotonic())
    assert isinstance(raised.value.error, OmnigraphConflict)


def test_a_missing_parameter_is_a_rejection_before_anything_is_sent(remote):
    client, answer = remote
    answer(raises=AssertionError("nothing may be sent"))
    with pytest.raises(RejectedBatch) as raised:
        client._commit_group(
            [STEPS[0], ("m.gq", "put", {})], None, False, time.monotonic()
        )
    assert isinstance(raised.value.error, KeyError)
