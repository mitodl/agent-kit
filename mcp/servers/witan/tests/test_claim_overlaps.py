"""The overlap advisory a first claim carries: other live work on the same code."""

import pytest

from witan import readiness

from .conftest import requires_omnigraph

REPO = "https://github.com/test/repo"
RUN = f"{REPO}#a.py::run"
STOP = f"{REPO}#a.py::stop"
OTHER = f"{REPO}#b.py::other"


@pytest.fixture
def logged(server, monkeypatch):
    """The fields of each ``info`` line the server logs, by event name."""
    events: dict[str, list[dict]] = {}

    class Recorder:
        def info(self, event, **fields):
            events.setdefault(event, []).append(fields)

        def __getattr__(self, _name):
            return lambda *_args, **_fields: None

    monkeypatch.setattr(server._module, "logger", Recorder())
    return events


def _task(server, refs, title="t"):
    return server.task_create(title=title, description="d", symbol_refs=refs)


def _held(server, refs, title="live"):
    task = _task(server, refs, title=title)
    server.task_claim(task["slug"], assignee="someone-else")
    return task


@requires_omnigraph
def test_a_claim_sharing_a_symbol_with_live_work_names_it(server):
    live = _held(server, [RUN, OTHER])
    task = _task(server, [RUN])

    claimed = server.task_claim(task["slug"])

    assert claimed["overlaps"] == [
        {
            "slug": live["slug"],
            "title": "live",
            "assignee": "someone-else",
            "claimed_at": server.task_get(live["slug"])["claimed_at"],
            "symbols": [RUN],
            "files": [f"{REPO}#a.py"],
        }
    ]


@requires_omnigraph
def test_a_claim_with_no_overlap_returns_none(server):
    _held(server, [OTHER])
    task = _task(server, [RUN])

    assert server.task_claim(task["slug"])["overlaps"] == []


@requires_omnigraph
def test_a_task_without_refs_returns_none(server):
    _held(server, [RUN])
    task = server.task_create(title="t", description="d")

    assert server.task_claim(task["slug"])["overlaps"] == []


@requires_omnigraph
def test_sharing_only_a_file_is_reported_after_sharing_a_symbol(server):
    same_file = _held(server, [STOP], title="same file")
    same_symbol = _held(server, [RUN], title="same symbol")
    task = _task(server, [RUN])

    overlaps = server.task_claim(task["slug"])["overlaps"]

    assert [(o["slug"], o["symbols"]) for o in overlaps] == [
        (same_symbol["slug"], [RUN]),
        (same_file["slug"], []),
    ]


@requires_omnigraph
def test_open_closed_and_lapsed_tasks_are_not_in_flight(server, monkeypatch):
    _task(server, [RUN], title="open")
    closed = _held(server, [RUN], title="closed")
    server.task_close(closed["slug"], resolution="done")
    _held(server, [RUN], title="lapsed")
    monkeypatch.setattr(readiness, "CLAIM_LEASE_SECONDS", -1)
    task = _task(server, [RUN])

    assert server.task_claim(task["slug"])["overlaps"] == []


@requires_omnigraph
def test_a_renewal_does_not_repeat_it(server):
    _held(server, [RUN])
    task = _task(server, [RUN])
    server.task_claim(task["slug"])

    assert "overlaps" not in server.task_claim(task["slug"])


@requires_omnigraph
def test_a_stolen_claim_carries_it(server):
    """A session taking over a task has seen nothing, unlike one renewing it."""
    live = _held(server, [RUN])
    task = _task(server, [RUN])
    server.task_claim(task["slug"], assignee="third")

    claimed = server.task_claim(task["slug"], force=True)

    assert [o["slug"] for o in claimed["overlaps"]] == [live["slug"]]


@requires_omnigraph
def test_a_failed_lookup_does_not_fail_a_claim_that_won(server, monkeypatch):
    def broken(_task):
        raise RuntimeError("omnigraph down")

    monkeypatch.setattr(server._module, "_task_overlaps", broken)
    _held(server, [RUN])
    task = _task(server, [RUN])

    claimed = server.task_claim(task["slug"])

    assert claimed["claimed"] is True
    assert claimed["overlaps"] == []
    assert server.task_get(task["slug"])["status"] == "in_progress"


@requires_omnigraph
def test_every_first_claim_is_logged_including_empty_ones(server, logged):
    """The share of claims that found an overlap needs the ones that found none."""
    live = _held(server, [RUN])
    task = _task(server, [RUN])
    lone = _task(server, [OTHER])

    server.task_claim(task["slug"])
    server.task_claim(lone["slug"])

    assert logged["witan.claim.overlaps"][-2:] == [
        {
            "task": task["slug"],
            "session": None,
            "count": 1,
            "slugs": [live["slug"]],
            "shared_symbols": 1,
        },
        {
            "task": lone["slug"],
            "session": None,
            "count": 0,
            "slugs": [],
            "shared_symbols": 0,
        },
    ]
