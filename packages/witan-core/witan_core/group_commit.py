"""Group commit: concurrent writes to one graph share one omnigraph commit.

On omnigraph 0.13 commits to one branch serialize, at about 1s each against the
deployed S3 store, so one commit per write caps a graph near one write a
second however many callers are waiting. A composed mutation costs a few
milliseconds per extra statement on top of that fixed cost, so the writes that
queue up behind a commit in flight are committed together by the next one.

LEADER/FOLLOWER, NO TIMER. A write on an idle lane commits at once, alone, so
an unloaded graph pays nothing for this. A write that arrives while a commit is
in flight waits; when that commit returns, the oldest waiting write leads the
next commit and takes everything queued behind it (up to the caps). One commit
is in flight per lane at a time.

WHAT A GROUP MAY HOLD, and why each rule preserves what a caller was promised:

* One actor. A lane is keyed by the bearer token, among other things, because
  omnigraph authorizes a request once and records one ``actor_id`` on its
  commit. Grouping two actors would write B's change as A.
* One class of statement. omnigraph rejects a query that mixes inserts or
  updates with deletes, so a group takes the class of its oldest member and the
  other class waits a round.
* At most one conditional write (``if_commit`` or ``surface_conflict``),
  committed FIRST, with the group fenced on its head. That is the fenced write
  committing alone at the head it read, followed by unconditional upserts, so
  its guarantee (nothing changed since its read) is exactly what it was.
  Two fenced writes cannot share a commit: a request takes one precondition,
  and proving the second's read still holds after the first's writes needs its
  read set, which this layer does not have.

PER-CALLER OUTCOMES. Success gives every member the group's commit id. A
:class:`ConditionFailed` (a refused precondition or a surfaced conflict) goes
to the conditional member only, and the others go back to the head of the
queue for the next commit. A :class:`RejectedBatch` (the server answered and
applied nothing) is replayed one member at a time, so one caller's bad write
fails only that caller. Any other failure goes to every member unchanged. Each
caller then has the retry-safety it would have had alone, but one lost response
leaves every member of that group to re-read before retrying.

KNOWN-STALE FENCES. Each commit's response names the head it replaced. A queued
fenced write whose fence is one of those heads would be refused by the server,
and would cost the writes riding with it a commit, so it is refused here
instead. A wrong guess here can only refuse a write (the caller re-reads, as on
a 412), never apply one.

EXPIRED WRITES. A write whose own deadline passes while it queues is refused
before its group is sent, as the write gate refuses one it cannot finish in
time: committing it would land a write its caller has stopped waiting for.

Stdlib only, like the rest of ``witan_core``'s base modules.
"""

from __future__ import annotations

import copy
import threading
import time
from collections import deque
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field

#: ``(query_file, query_name, params)``, the triple ``change_many`` takes.
Step = tuple[str, str, dict]

#: ``commit(steps, if_commit, surface_conflict, started) -> (commit_id,
#: parent_id)``. ``started`` is the monotonic time the call's budget runs from.
CommitFn = Callable[
    [list[Step], "str | None", bool, float], tuple["str | None", "str | None"]
]

#: Statements and estimated param bytes in one group commit. omnigraph-server
#: refuses a body over 1 MiB, and a composed statement adds about 1.5 KB of
#: source on top of its params, so 64 statements and 256 KiB of params stay
#: well under it.
MAX_GROUP_STATEMENTS = 64
MAX_GROUP_BYTES = 256 * 1024

#: How many replaced heads a lane remembers for the known-stale check.
_SUPERSEDED_MEMORY = 64
#: How many recent group sizes ``GroupCommitter.commits`` keeps.
_RECENT_COMMITS = 1024


class RejectedBatch(Exception):
    """Raised by a ``CommitFn`` when the server answered and applied nothing.

    Only then is replaying members one at a time safe; a failure whose effects
    are unknown must not be replayed, or a write that landed lands twice.
    """

    def __init__(self, error: BaseException) -> None:
        super().__init__(str(error))
        self.error = error


class ConditionFailed(Exception):
    """Raised by a ``CommitFn`` when the group's precondition was refused.

    The precondition is the conditional member's, so ``error`` is its outcome
    alone and nothing was written for anyone.
    """

    def __init__(self, error: BaseException) -> None:
        super().__init__(str(error))
        self.error = error


@dataclass(eq=False)
class _Member:
    steps: list[Step]
    if_commit: str | None
    surface_conflict: bool
    destructive: bool
    cost: int
    deadline: float | None
    submitted: float = field(default_factory=time.monotonic)
    lead: bool = False
    done: bool = False
    result: str | None = None
    error: BaseException | None = None

    @property
    def conditional(self) -> bool:
        return self.if_commit is not None or self.surface_conflict


@dataclass(eq=False)
class _Lane:
    queue: deque[_Member] = field(default_factory=deque)
    superseded: deque[str] = field(
        default_factory=lambda: deque(maxlen=_SUPERSEDED_MEMORY)
    )


class GroupCommitter:
    """Process-wide lanes of queued writes, one lane per (actor, graph).

    The two error types are passed in so this module does not import the
    client that imports it.

    :param conflict_type: what a caller that surfaces conflicts gets for a
        refused precondition (``OmnigraphConflict``).
    :param expired_type: what a write whose deadline passed in the queue gets
        (``WriteQueueFull``).
    :param max_statements: statement cap for one group commit.
    :param max_bytes: estimated param-byte cap for one group commit.
    """

    def __init__(
        self,
        conflict_type: Callable[[str], BaseException],
        expired_type: Callable[[str], BaseException],
        *,
        max_statements: int = MAX_GROUP_STATEMENTS,
        max_bytes: int = MAX_GROUP_BYTES,
    ) -> None:
        self._conflict_type = conflict_type
        self._expired_type = expired_type
        self.max_statements = max_statements
        self.max_bytes = max_bytes
        self._cv = threading.Condition()
        self._lanes: dict[Hashable, _Lane] = {}
        #: Recent commits issued, by group size. For tests and benchmarks.
        self.commits: deque[int] = deque(maxlen=_RECENT_COMMITS)

    def submit(
        self,
        key: Hashable,
        steps: list[Step],
        commit: CommitFn,
        *,
        if_commit: str | None = None,
        surface_conflict: bool = False,
        destructive: bool = False,
        cost: int = 0,
        budget: float | None = None,
    ) -> str | None:
        """Commit ``steps``, alone or with whatever else is queued on ``key``.

        Blocks until this write's own outcome is known, and returns the commit
        id it landed in (``None`` when the transport reports none) or raises
        the error that belongs to it.

        :param key: the lane. Every write on a lane must be committable by any
            other's ``commit``: same graph, same credentials, same queries.
        :param steps: this caller's statements, in order, guard already applied.
        :param commit: commits a list of steps as one mutation.
        :param if_commit: the head this write is fenced on.
        :param surface_conflict: raise a conflict instead of retrying it.
        :param destructive: whether any step deletes.
        :param cost: estimated bytes of params, for the group cap.
        :param budget: seconds this call has, from now, or ``None`` for no limit.
        :returns: the commit id this write landed in, if known.
        :rtype: str | None
        """
        now = time.monotonic()
        member = _Member(
            steps=steps,
            if_commit=if_commit,
            surface_conflict=surface_conflict,
            destructive=destructive,
            cost=cost,
            deadline=now + budget if budget else None,
            submitted=now,
        )
        with self._cv:
            lane = self._lanes.get(key)
            if lane is None:
                lane = self._lanes[key] = _Lane()
                member.lead = True
            lane.queue.append(member)
            while not (member.lead or member.done):
                self._cv.wait()
        if not member.done:
            self._lead(key, lane, member, commit)
        if member.error is not None:
            own = _own_copy(member.error)
            if own is member.error:
                raise own
            raise own from member.error
        return member.result

    def _lead(self, key: Hashable, lane: _Lane, me: _Member, commit: CommitFn) -> None:
        """Commit groups until ``me`` is done, then hand the lane on."""
        while True:
            with self._cv:
                if me.done:
                    if lane.queue:
                        lane.queue[0].lead = True
                    else:
                        del self._lanes[key]
                    self._cv.notify_all()
                    return
                group = self._take(lane)
                self._cv.notify_all()
            if group:
                self._run(lane, group, commit)

    def _take(self, lane: _Lane) -> list[_Member]:
        """Remove and return the next group from ``lane``. Caller holds the lock.

        The conditional member, if any, comes first; the rest keep queue order.
        """
        picked: list[_Member] = []
        conditional: _Member | None = None
        destructive: bool | None = None
        statements = size = 0
        now = time.monotonic()
        for member in list(lane.queue):
            if member.deadline is not None and now >= member.deadline:
                lane.queue.remove(member)
                self._finish(member, error=self._expired(member, now))
                continue
            if member.conditional:
                if member.if_commit is not None and member.if_commit in lane.superseded:
                    lane.queue.remove(member)
                    self._finish(member, error=self._stale(member))
                    continue
                if conditional is not None:
                    continue
            if destructive is None:
                destructive = member.destructive
            elif member.destructive != destructive:
                continue
            if picked and (
                statements + len(member.steps) > self.max_statements
                or size + member.cost > self.max_bytes
            ):
                break
            picked.append(member)
            statements += len(member.steps)
            size += member.cost
            if member.conditional:
                conditional = member
        for member in picked:
            lane.queue.remove(member)
        if conditional is not None and picked[0] is not conditional:
            picked.remove(conditional)
            picked.insert(0, conditional)
        return picked

    def _stale(self, member: _Member) -> BaseException:
        message = (
            f"precondition failed: expected head '{member.if_commit}', which a "
            "commit this process made has already replaced. NOTHING WAS "
            "WRITTEN; re-read and decide."
        )
        if member.surface_conflict:
            return self._conflict_type(message)
        return RuntimeError(message)

    def _expired(self, member: _Member, now: float) -> BaseException:
        return self._expired_type(
            f"write was refused before it was sent: it queued "
            f"{now - member.submitted:.0f}s behind other writes to this graph "
            "and its call has no time left. NOTHING WAS WRITTEN — retry once "
            "the burst clears."
        )

    def _run(self, lane: _Lane, group: list[_Member], commit: CommitFn) -> None:
        """Commit ``group`` and deliver every member's outcome. Never raises."""
        lead = group[0]
        conditional = lead if lead.conditional else None
        started = min(member.submitted for member in group)
        steps = [step for member in group for step in member.steps]
        self.commits.append(len(group))
        try:
            commit_id, parent_id = commit(
                steps,
                conditional.if_commit if conditional else None,
                conditional.surface_conflict if conditional else False,
                started,
            )
        except RejectedBatch as rejected:
            if len(group) == 1:
                self._finish(lead, error=rejected.error)
                return
            for member in group:
                self._run(lane, [member], commit)
            return
        except ConditionFailed as failed:
            with self._cv:
                if conditional is not None:
                    self._finish(conditional, error=failed.error)
                    lane.queue.extendleft(reversed(group[1:]))
                else:
                    for member in group:
                        self._finish(member, error=failed.error)
            return
        except BaseException as exc:  # noqa: BLE001 — each caller re-raises it
            for member in group:
                self._finish(member, error=exc)
            return
        with self._cv:
            if parent_id is not None:
                lane.superseded.append(parent_id)
            for member in group:
                self._finish(member, result=commit_id)

    def _finish(
        self,
        member: _Member,
        *,
        result: str | None = None,
        error: BaseException | None = None,
    ) -> None:
        with self._cv:
            member.result = result
            member.error = error
            member.done = True
            self._cv.notify_all()


def _own_copy(error: BaseException) -> BaseException:
    """A copy of ``error`` for one caller to raise.

    The same instance raised from several threads would have its traceback
    rewritten by each. An exception whose constructor cannot be replayed from
    its ``args`` (``json.JSONDecodeError`` is one) is raised as the original.
    """
    try:
        return copy.copy(error)
    except Exception:  # noqa: BLE001 — the original is the fallback
        return error
