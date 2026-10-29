"""Group commit against a real omnigraph-server 0.13, through witan's tools.

The unit suite (packages/witan-core/tests/test_group_commit.py) proves the
lane logic with a fake store. These run the same concurrent shapes through the
real tools and a real served graph, which is the only way to show that a
composed group is a mutation omnigraph accepts, that a fenced claim's
guarantee survives sharing a commit, and how many commits a burst costs.

Needs ``omnigraph-server``; see the ``served`` fixture in conftest.py.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest
from testsupport.omnigraph_server import requires_omnigraph_server

from .conftest import SERVED_GRAPH, SERVED_REPO

GRAPH = SERVED_GRAPH
REPO = SERVED_REPO
pytestmark = requires_omnigraph_server


def _commit_count(url: str) -> int:
    request = urllib.request.Request(
        f"{url}/graphs/{GRAPH}/commits", headers={"omnigraph-http-api": "0.13"}
    )
    with urllib.request.urlopen(request) as response:
        return len(json.loads(response.read())["commits"])


def _queued() -> int:
    from witan_core import omnigraph as og

    return sum(len(lane.queue) for lane in og._GROUP_COMMITTER._lanes.values())


@pytest.fixture
def burst(monkeypatch):
    """Run one call, hold its commit, queue the rest behind it, release.

    Without the hold the test would depend on scheduling: a local commit takes
    ~10ms, so whether a burst groups at all is a race. Holding it makes the
    queue the test describes the queue the committer sees. ``hold="before"``
    holds the commit before it is sent, ``"after"`` once it has published but
    before its caller hears back. Returns each call's result or exception, in
    order, and the group sizes committed meanwhile; ``burst.calls`` has each
    commit's step names and fence.
    """
    from witan_core import omnigraph as og
    from witan_core.group_commit import GroupCommitter

    monkeypatch.setattr(
        og, "_GROUP_COMMITTER", GroupCommitter(og.OmnigraphConflict, og.WriteQueueFull)
    )
    real = og.OmnigraphClient._commit_group
    gate = {"expect": 0, "held": True, "hold": "before"}
    in_flight = threading.Event()

    def wait_for_queue():
        in_flight.set()
        deadline = time.monotonic() + 20
        while _queued() < gate["expect"]:
            assert time.monotonic() < deadline, "writes never queued"
            time.sleep(0.005)

    def held(self, steps, if_commit, surface_conflict, started):
        run.calls.append(([name for _, name, _ in steps], if_commit))
        first = not gate["held"]
        gate["held"] = True
        if first and gate["hold"] == "before":
            wait_for_queue()
        result = real(self, steps, if_commit, surface_conflict, started)
        if first and gate["hold"] == "after":
            wait_for_queue()
        return result

    monkeypatch.setattr(og.OmnigraphClient, "_commit_group", held)

    def run(first, rest, hold="before"):
        gate.update(expect=len(rest), hold=hold, held=False)
        run.calls.clear()
        sizes_before = len(og._GROUP_COMMITTER.commits)
        outcomes: list = [None] * (1 + len(rest))

        def call(i, fn):
            try:
                outcomes[i] = fn()
            except Exception as exc:  # noqa: BLE001 — returned to the test
                outcomes[i] = exc

        threads = [threading.Thread(target=call, args=(0, first))]
        threads[0].start()
        assert in_flight.wait(20)
        for i, fn in enumerate(rest, start=1):
            threads.append(threading.Thread(target=call, args=(i, fn)))
            threads[-1].start()
        for thread in threads:
            thread.join(60)
            assert not thread.is_alive()
        return outcomes, list(og._GROUP_COMMITTER.commits)[sizes_before:]

    run.calls = []
    return run


def test_a_burst_of_creates_lands_in_two_commits(served, burst):
    tools, url = served
    before = _commit_count(url)

    def create(i):
        return lambda: tools.task_create(title=f"burst {i}", description="d", repo=REPO)

    created, sizes = burst(create(0), [create(i) for i in range(1, 16)])

    assert sizes == [1, 15]
    assert _commit_count(url) - before == 2
    for task in created:
        assert tools.task_get(task["slug"])["title"].startswith("burst ")


def test_concurrent_claims_still_grant_exactly_one(served, burst):
    """Eight sessions claim one task while unrelated comments queue too.

    Every claim reads the task as open before any of them commits. The first
    commits; the other seven were fenced on the head it replaced, so they are
    refused without a round trip, re-read, and find it held. The comments ride
    in one commit whichever way the claims go.
    """
    tools, url = served
    task = tools.task_create(title="contested", description="d", repo=REPO)
    other = tools.task_create(title="chatter", description="d", repo=REPO)
    before = _commit_count(url)

    def claim(agent):
        return lambda: tools.task_claim(task["slug"], assignee=agent)

    def comment(text):
        return lambda: tools.task_comment(other["slug"], text)

    notes = [f"note {i}" for i in range(6)]
    outcomes, sizes = burst(
        claim("agent-0"),
        [claim(f"agent-{i}") for i in range(1, 8)] + [comment(n) for n in notes],
    )

    claims = outcomes[:8]
    assert not [c for c in outcomes if isinstance(c, Exception)], outcomes
    assert [c["claimed"] for c in claims] == [True] + [False] * 7, claims
    assert {c["reason"] for c in claims[1:]} <= {"held", "lost_race"}
    assert {c["held_by"] for c in claims[1:]} == {"agent-0"}
    assert tools.task_get(task["slug"])["assignee"] == "agent-0"
    assert sorted(c["body"] for c in tools.task_get(other["slug"])["comments"]) == notes
    # The claim, then one commit for all six comments. task_claim's CodeBranch
    # write follows the claim as its own commit on main, so it may add one.
    assert 6 in sizes, sizes
    assert _commit_count(url) - before <= 3


def test_a_fresh_fenced_write_leads_the_writes_queued_with_it(served, burst):
    """A fenced write that read the head the in-flight commit just published
    shares the next commit with the writes queued beside it, first in it and
    fenced on that head.

    Held after the first commit publishes and before its caller hears back:
    the window in which a read sees the new head while the lane is still busy.
    """
    tools, url = served
    task = tools.task_create(title="t", description="d", repo=REPO)
    other = tools.task_create(title="chatter", description="d", repo=REPO)
    before = _commit_count(url)

    def comment(text):
        return lambda: tools.task_comment(other["slug"], text)

    outcomes, sizes = burst(
        comment("held"),
        [
            lambda: tools.task_update(task["slug"], priority="p0"),
            comment("rider 1"),
            comment("rider 2"),
        ],
        hold="after",
    )

    assert not [o for o in outcomes if isinstance(o, Exception)], outcomes
    assert sizes == [1, 3]
    assert burst.calls[1][0][0] == "update_task"
    assert burst.calls[1][1] is not None, "the group is fenced"
    assert _commit_count(url) - before == 2
    assert tools.task_get(task["slug"])["priority"] == "p0"
    bodies = {c["body"] for c in tools.task_get(other["slug"])["comments"]}
    assert bodies == {"held", "rider 1", "rider 2"}


def test_a_rejected_write_fails_only_its_own_caller(served, burst):
    """A statement the server refuses (400) sinks a composed group whole, so
    the group is replayed one write at a time and only its caller sees it."""
    from witan import server as srv

    tools, _url = served

    def create(title):
        return lambda: tools.task_create(title=title, description="d", repo=REPO)

    def bad():
        srv.client.change(
            "mutations.gq",
            "link_blocks",
            {"from": "tk-missing-a", "to": "tk-missing-b"},
        )

    outcomes, sizes = burst(create("first"), [create("a"), bad, create("b")])

    assert "not found" in str(outcomes[2])
    assert [isinstance(o, Exception) for o in outcomes] == [False, False, True, False]
    assert sizes == [1, 3, 1, 1, 1]
    assert {t["title"] for t in tools.task_list(repo=REPO)} >= {"first", "a", "b"}


def test_a_missing_parameter_fails_only_its_own_caller(served, burst):
    """Caught while composing the group, before anything is sent, and
    replayed so that only the write missing it fails."""
    from witan import server as srv

    tools, _url = served

    def create(title):
        return lambda: tools.task_create(title=title, description="d", repo=REPO)

    def incomplete():
        srv.client.change_many(
            [
                ("mutations.gq", "link_blocks", {"from": "x", "to": "y"}),
                ("mutations.gq", "link_blocks", {"from": "x"}),
            ]
        )

    outcomes, sizes = burst(create("first"), [create("a"), incomplete, create("b")])

    assert isinstance(outcomes[2], KeyError)
    assert [isinstance(o, Exception) for o in outcomes] == [False, False, True, False]
    assert sizes == [1, 3, 1, 1, 1]


def test_a_server_refused_fence_spares_the_writes_riding_with_it(served, burst):
    """A fence the lane cannot know is stale (it read before the lane's last
    idle spell) reaches the server, which answers 412. Only that caller is
    refused, and with the error it would have had alone."""
    from witan import server as srv

    tools, _url = served
    task = tools.task_create(title="t", description="d", repo=REPO)
    _, old_head = srv.client.read_with_commit(
        "read.gq", "get_task", {"slug": task["slug"]}
    )
    other = tools.task_create(title="moves the head", description="d", repo=REPO)
    row = tools.task_get(task["slug"])

    def fenced_on_old_head():
        params = {
            k: row.get(k)
            for k in (
                "slug",
                "title",
                "description",
                "type",
                "status",
                "priority",
                "repo",
                "project_slug",
                "parent_slug",
                "blocked_by",
                "assignee",
                "external_uri",
                "resolution",
                "symbol_refs",
                "tags",
                "closed_at",
                "claimed_at",
                "first_claimed_at",
                "updated_at",
            )
        }
        srv.client.change("mutations.gq", "update_task", params, if_commit=old_head)

    def comment(text):
        return lambda: tools.task_comment(other["slug"], text)

    outcomes, sizes = burst(
        comment("held"), [fenced_on_old_head, comment("r1"), comment("r2")]
    )

    assert type(outcomes[1]) is RuntimeError
    assert "precondition" in str(outcomes[1])
    assert [isinstance(o, Exception) for o in outcomes] == [False, True, False, False]
    assert sizes == [1, 3, 2]
    bodies = {c["body"] for c in tools.task_get(other["slug"])["comments"]}
    assert bodies == {"held", "r1", "r2"}
