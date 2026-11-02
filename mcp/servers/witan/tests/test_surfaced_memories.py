"""Memories that reach a session without the agent asking for them."""

import pytest

from witan.config import RankConfig

from .conftest import requires_omnigraph


@pytest.fixture
def no_floor(server, monkeypatch):
    """Scores on a handful of rows are tiny, so the floors are tested on their own."""
    monkeypatch.setattr(
        server._module,
        "rank_cfg",
        RankConfig(surface_min_content_score=0.0, surface_min_title_score=0.0),
    )


def _task(server, title="frobnicate the widget cache"):
    return server.task_create(title=title, description="d")


def _memory(server, title, content):
    return server.memory_store(kind="pattern", title=title, content=content)


@requires_omnigraph
def test_a_first_claim_carries_the_memories_its_title_matches(server, no_floor):
    match = _memory(server, "frobnicate widget cache", "how to frobnicate a widget")
    task = _task(server)

    claimed = server.task_claim(task["slug"])

    assert [m["slug"] for m in claimed["memories"]] == [match["slug"]]
    assert set(claimed["memories"][0]) == {"slug", "kind", "title", "excerpt"}


@requires_omnigraph
def test_a_memory_needs_a_match_in_both_title_and_content(server, no_floor):
    """A long memory mentions most ordinary words, which says nothing about what
    it is about. Its title does."""
    _memory(server, "unrelated heading", "frobnicate widget cache in passing")
    _memory(server, "frobnicate widget cache", "nothing relevant in this body")

    assert server._title_matches("frobnicate widget cache") == []


@requires_omnigraph
def test_a_weak_match_shows_nothing(server):
    """The floors are raw scores. Relevance is relative to the run's best, so it
    could never say the best was weak."""
    _memory(server, "frobnicate widget cache", "how to frobnicate a widget")
    task = _task(server)

    assert "memories" not in server.task_claim(task["slug"])


@requires_omnigraph
def test_a_lease_renewal_does_not_repeat_them(server, no_floor):
    _memory(server, "frobnicate widget cache", "how to frobnicate a widget")
    task = _task(server)
    server.task_claim(task["slug"])

    assert "memories" not in server.task_claim(task["slug"])


@requires_omnigraph
def test_linked_memories_come_before_title_matches(server, no_floor):
    linked = _memory(server, "unrelated heading", "nothing in common")
    match = _memory(server, "frobnicate widget cache", "how to frobnicate a widget")
    task = _task(server)
    server.task_link(task["slug"], linked["slug"], "addresses")

    claimed = server.task_claim(task["slug"])

    assert [m["slug"] for m in claimed["memories"]] == [linked["slug"], match["slug"]]


@requires_omnigraph
def test_a_superseded_memory_is_not_surfaced(server, no_floor):
    old = _memory(server, "frobnicate widget cache", "the old way to frobnicate")
    new = _memory(server, "frobnicate widget cache again", "frobnicate the new way")
    server.memory_link(new["slug"], old["slug"], "supersedes")
    task = _task(server)

    claimed = server.task_claim(task["slug"])

    assert [m["slug"] for m in claimed["memories"]] == [new["slug"]]


@requires_omnigraph
def test_at_most_a_few_are_surfaced_as_excerpts(server, no_floor, monkeypatch):
    monkeypatch.setattr(server._module, "_RESULT_EXCERPT_CHARS", 40)
    for i in range(5):
        _memory(server, f"frobnicate widget {i}", f"frobnicate widget {i} " * 20)
    task = _task(server)

    memories = server.task_claim(task["slug"])["memories"]

    assert len(memories) == server._module._SURFACED_MEMORY_LIMIT
    assert all(len(m["excerpt"]) == 40 for m in memories)


@requires_omnigraph
def test_a_failed_lookup_does_not_fail_a_claim_that_won(server, monkeypatch):
    """The claim has committed by then, so an error would report a held task as
    a failed claim."""

    def broken(_task):
        raise RuntimeError("omnigraph down")

    monkeypatch.setattr(server._module, "_task_memories", broken)
    task = _task(server)

    claimed = server.task_claim(task["slug"])

    assert claimed["claimed"] is True
    assert "memories" not in claimed
    assert server.task_get(task["slug"])["status"] == "in_progress"


@requires_omnigraph
def test_every_first_claim_is_logged_including_empty_ones(server, monkeypatch):
    """The number that says whether this works is the share of claims that
    carried a memory, which needs the claims that carried none."""

    class Recorder:
        def __init__(self):
            self.events = []

        def info(self, event, **fields):
            self.events.append((event, fields))

        def __getattr__(self, _name):
            return lambda *_args, **_fields: None

    recorder = Recorder()
    monkeypatch.setattr(server._module, "logger", recorder)
    task = _task(server)

    server.task_claim(task["slug"])
    server.task_claim(task["slug"])

    assert [f for e, f in recorder.events if e == "witan.memory.surfaced"] == [
        {"surface": "task_claim", "task": task["slug"], "count": 0, "slugs": []}
    ]


@requires_omnigraph
def test_recall_by_task_finds_what_the_title_matches(server, no_floor):
    match = _memory(server, "frobnicate widget cache", "how to frobnicate a widget")
    task = _task(server)

    out = server.recall(task=task["slug"], hops=0)

    assert [m["slug"] for m in out["memories"]] == [match["slug"]]
    assert out["seeds"] == {"task": [match["slug"]]}


@requires_omnigraph
def test_recall_by_task_ranks_a_linked_memory_above_a_title_match(server, no_floor):
    linked = _memory(server, "unrelated heading", "nothing in common")
    match = _memory(server, "frobnicate widget cache", "how to frobnicate a widget")
    task = _task(server)
    server.task_link(task["slug"], linked["slug"], "addresses")

    out = server.recall(task=task["slug"], hops=0)

    assert [m["slug"] for m in out["memories"]] == [linked["slug"], match["slug"]]


def test_the_task_prompt_carries_memories_when_there_are_some():
    from witan.cli.run_helpers import _run_prompt

    prompt = _run_prompt(
        {"slug": "tk-a", "title": "T", "type": "bug"},
        claimed=True,
        memories=[{"slug": "les-a", "title": "A lesson", "content": "one\n\ntwo"}],
    )

    assert "memory_get has the full text" in prompt
    assert "les-a: A lesson" in prompt
    assert "one two" in prompt


def test_the_task_prompt_says_nothing_about_memories_when_there_are_none():
    from witan.cli.run_helpers import _run_prompt

    prompt = _run_prompt({"slug": "tk-a", "title": "T"}, claimed=True, memories=[])

    assert "memory_get" not in prompt


def test_a_failed_memory_read_does_not_stop_the_agent_launching():
    from witan.cli.run_helpers import _prompt_memories

    class Broken:
        def recall(self, **_kwargs):
            raise RuntimeError("deployment unreachable")

    assert _prompt_memories(Broken(), "tk-a") == []
