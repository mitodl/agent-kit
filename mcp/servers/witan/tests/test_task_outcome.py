"""A fan-out worker's recorded outcome: writing it, reading it back, requeueing."""

from __future__ import annotations

import typing

import pytest
from pydantic import ValidationError

from witan import outcome as outcome_mod
from witan import readiness
from witan.cli import _common
from witan.cli._common import _fn

from .conftest import requires_omnigraph

pytestmark = pytest.mark.usefixtures("outside_a_checkout")

WORKER = "pytest#worker01"
READY = {
    "outcome": "ready_for_review",
    "branch": "fanout/tk-x",
    "commits": ["0123abcd", "4567ef01"],
    "check_output": "42 passed",
}


def _claimed(server, title: str = "fan-out work", assignee: str = WORKER) -> str:
    slug = server.task_create(title=title, description="x")["slug"]
    assert server.task_claim(slug, assignee=assignee)["claimed"] is True
    return slug


def _ready_slugs(server) -> set[str]:
    return {r["slug"] for r in server.task_ready()}


def test_the_three_spellings_of_the_choice_set_agree():
    from witan import server as srv

    values = {k.value for k in outcome_mod.OutcomeKind}
    assert set(typing.get_args(srv.TaskOutcomeKind)) == values
    assert set(typing.get_args(_common.TaskOutcomeKind)) == values


@pytest.mark.parametrize(
    ("fields", "missing"),
    [
        (
            {"outcome": "ready_for_review", "branch": "b", "commits": ["c"]},
            "check_output",
        ),
        (
            {"outcome": "ready_for_review", "branch": "b", "check_output": "ok"},
            "commits",
        ),
        ({**READY, "branch": "  "}, "branch"),
        ({**READY, "commits": ["", " "]}, "commits"),
        ({"outcome": "needs_human"}, "the question"),
        ({"outcome": "cannot_proceed", "detail": "\n"}, "the reason"),
    ],
)
def test_each_outcome_requires_its_evidence(fields, missing):
    with pytest.raises(ValidationError, match=missing):
        outcome_mod.TaskOutcome(**fields)


def test_an_unknown_outcome_is_refused():
    with pytest.raises(ValidationError):
        outcome_mod.TaskOutcome(outcome="done", detail="x")


def test_a_task_with_an_outcome_is_never_pickable():
    assert readiness.status_pickable({"status": "open"}) is True
    assert (
        readiness.status_pickable({"status": "open", "outcome": "needs_human"}) is False
    )


@requires_omnigraph
@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            READY,
            {
                "outcome_detail": None,
                "outcome_branch": "fanout/tk-x",
                "outcome_commits": ["0123abcd", "4567ef01"],
                "outcome_check": "42 passed",
            },
        ),
        (
            {
                "outcome": "needs_human",
                "detail": "Which region?",
                "branch": "fanout/tk-x",
            },
            {
                "outcome_detail": "Which region?",
                "outcome_branch": "fanout/tk-x",
                "outcome_commits": None,
                "outcome_check": None,
            },
        ),
        (
            {"outcome": "cannot_proceed", "detail": "The API it needs does not exist."},
            {
                "outcome_detail": "The API it needs does not exist.",
                "outcome_branch": None,
                "outcome_commits": None,
                "outcome_check": None,
            },
        ),
    ],
    ids=["ready_for_review", "needs_human", "cannot_proceed"],
)
def test_a_worker_records_each_outcome_and_it_reads_back(server, fields, expected):
    slug = _claimed(server)

    result = server.task_record_outcome(slug, assignee=WORKER, **fields)

    assert result["recorded"] is True
    assert result["outcome"] == fields["outcome"]
    assert result["outcome_set_by"] == WORKER
    task = server.task_get(slug)
    assert task["outcome"] == fields["outcome"]
    assert {k: task[k] for k in expected} == expected
    assert task["outcome_set_by"] == WORKER
    assert task["outcome_set_at"]
    # The claim ends with the attempt.
    assert task["status"] == "open"
    assert task["assignee"] is None
    assert task["claimed_at"] is None
    # ...and it waits on a person rather than going back to ready work.
    assert slug not in _ready_slugs(server)
    listed = {r["slug"]: r for r in server.task_list(all_repos=True)}
    assert listed[slug]["outcome"] == fields["outcome"]


@requires_omnigraph
def test_the_setter_is_the_session_qualified_holder(server):
    slug = server.task_create(title="t", description="x")["slug"]
    claim = server.task_claim(slug, session_id="sess-1")
    result = server.task_record_outcome(
        slug, outcome="needs_human", detail="q?", session_id="sess-1"
    )
    assert result["recorded"] is True
    assert server.task_get(slug)["outcome_set_by"] == claim["assignee"]
    assert "#" in claim["assignee"]


@requires_omnigraph
def test_recording_refuses_a_task_nobody_claimed(server):
    slug = server.task_create(title="t", description="x")["slug"]
    result = server.task_record_outcome(slug, outcome="cannot_proceed", detail="r")
    assert result["recorded"] is False
    assert result["reason"] == "not_claimed"
    assert server.task_get(slug)["outcome"] is None


@requires_omnigraph
def test_exactly_one_outcome_per_attempt(server):
    slug = _claimed(server)
    server.task_record_outcome(slug, assignee=WORKER, outcome="needs_human", detail="q")

    again = server.task_record_outcome(slug, assignee=WORKER, **READY)

    assert again["recorded"] is False
    assert again["reason"] == "not_claimed"
    assert again["outcome"] == "needs_human"
    assert "already carries needs_human" in again["remedy"]
    assert server.task_get(slug)["outcome"] == "needs_human"


@requires_omnigraph
def test_another_session_of_the_same_person_is_refused(server):
    """Runners act as the user, so a worker whose lease lapsed and was taken
    over is the same person in another session; it must not end the new
    holder's claim."""
    slug = _claimed(server, assignee="pytest#successor")

    result = server.task_record_outcome(
        slug, assignee="pytest#lapsed01", outcome="cannot_proceed", detail="r"
    )

    assert result["recorded"] is False
    assert result["reason"] == "held"
    assert result["held_by"] == "pytest#successor"
    task = server.task_get(slug)
    assert task["status"] == "in_progress"
    assert task["outcome"] is None


@requires_omnigraph
def test_force_records_over_another_holder(server):
    slug = _claimed(server, assignee="someone-else")

    result = server.task_record_outcome(
        slug, assignee=WORKER, outcome="cannot_proceed", detail="r", force=True
    )

    assert result["recorded"] is True
    task = server.task_get(slug)
    assert task["outcome_set_by"] == WORKER
    assert task["status"] == "open"


@requires_omnigraph
def test_a_forced_record_on_a_closed_task_leaves_it_closed(server):
    slug = server.task_create(title="t", description="x")["slug"]
    server.task_close(slug, resolution="done")

    server.task_record_outcome(slug, force=True, **READY)

    task = server.task_get(slug)
    assert task["status"] == "closed"
    assert task["outcome"] == "ready_for_review"


@requires_omnigraph
def test_an_invalid_outcome_writes_nothing(server):
    slug = _claimed(server)
    with pytest.raises(ValidationError):
        server.task_record_outcome(slug, assignee=WORKER, outcome="ready_for_review")
    task = server.task_get(slug)
    assert task["status"] == "in_progress"
    assert task["outcome"] is None


@requires_omnigraph
def test_a_claim_is_refused_until_the_task_is_requeued(server):
    slug = _claimed(server)
    server.task_record_outcome(slug, assignee=WORKER, outcome="needs_human", detail="q")

    refused = server.task_claim(slug, assignee="next-worker")

    assert refused["claimed"] is False
    assert refused["reason"] == "outcome"
    assert refused["outcome"] == "needs_human"
    assert f"witan task release {slug}" in refused["remedy"]

    released = server.task_release(slug)
    assert released["released"] is True
    assert released["cleared_outcome"] == "needs_human"
    task = server.task_get(slug)
    assert all(task[f] is None for f in outcome_mod.FIELDS)
    assert slug in _ready_slugs(server)
    assert server.task_claim(slug, assignee="next-worker")["claimed"] is True


@requires_omnigraph
def test_a_forced_claim_starts_a_new_attempt(server):
    slug = _claimed(server)
    server.task_record_outcome(slug, assignee=WORKER, outcome="needs_human", detail="q")

    assert server.task_claim(slug, assignee="next-worker", force=True)["claimed"]

    assert server.task_get(slug)["outcome"] is None


@requires_omnigraph
def test_an_unrelated_edit_and_a_close_keep_the_outcome(server):
    slug = _claimed(server)
    server.task_record_outcome(slug, assignee=WORKER, **READY)

    server.task_update(slug, title="renamed")
    assert server.task_get(slug)["outcome"] == "ready_for_review"

    server.task_close(slug, resolution="merged")
    task = server.task_get(slug)
    assert task["status"] == "closed"
    assert task["outcome"] == "ready_for_review"
    assert task["outcome_commits"] == READY["commits"]


@requires_omnigraph
def test_releasing_to_closed_keeps_the_outcome(server):
    slug = _claimed(server)
    server.task_record_outcome(slug, assignee=WORKER, **READY)

    released = server.task_release(slug, status="closed")

    assert "cleared_outcome" not in released
    assert server.task_get(slug)["outcome"] == "ready_for_review"


@requires_omnigraph
def test_reopening_a_closed_task_clears_the_outcome(server):
    slug = _claimed(server)
    server.task_record_outcome(slug, assignee=WORKER, **READY)
    server.task_close(slug, resolution="merged")

    server.task_update(slug, status="open")

    task = server.task_get(slug)
    assert task["outcome"] is None
    assert slug in _ready_slugs(server)


@requires_omnigraph
def test_witan_task_outcome_records_and_task_show_reads_it_back(
    server, monkeypatch, capsys
):
    from witan.cli.tasks import _task_show, task_outcome_cmd

    monkeypatch.setattr(_common, "_server", server)
    slug = _claimed(server)

    task_outcome_cmd(
        slug,
        "ready_for_review",
        branch="fanout/tk-x",
        commits=["0123abcd,4567ef01"],
        check_output="42 passed",
        assignee=WORKER,
    )
    assert "Recorded" in capsys.readouterr().out

    _task_show(slug)
    shown = capsys.readouterr().out
    assert "outcome: ready_for_review" in shown
    assert "fanout/tk-x" in shown
    assert "0123abcd, 4567ef01" in shown
    assert "42 passed" in shown
    assert _fn(server.task_get)(slug=slug)["outcome_commits"] == [
        "0123abcd",
        "4567ef01",
    ]


@requires_omnigraph
def test_witan_task_outcome_reads_the_check_from_a_file(
    server, monkeypatch, tmp_path, capsys
):
    from witan.cli.tasks import task_outcome_cmd

    monkeypatch.setattr(_common, "_server", server)
    slug = _claimed(server)
    check = tmp_path / "check.txt"
    check.write_text("== 7 passed ==\n")

    task_outcome_cmd(
        slug,
        "ready_for_review",
        branch="fanout/tk-x",
        commits=["0123abcd"],
        check_file=check,
        assignee=WORKER,
    )

    assert server.task_get(slug)["outcome_check"] == "== 7 passed ==\n"


@requires_omnigraph
def test_witan_task_outcome_names_what_is_missing(server, monkeypatch, capsys):
    from witan.cli.tasks import task_outcome_cmd

    monkeypatch.setattr(_common, "_server", server)
    slug = _claimed(server)

    with pytest.raises(SystemExit) as exc:
        task_outcome_cmd(slug, "needs_human", assignee=WORKER)

    assert exc.value.code == 2
    assert "the question" in capsys.readouterr().err
    assert server.task_get(slug)["status"] == "in_progress"


@requires_omnigraph
def test_witan_task_outcome_reports_a_refusal(server, monkeypatch, capsys):
    from witan.cli.tasks import task_outcome_cmd

    monkeypatch.setattr(_common, "_server", server)
    slug = _claimed(server, assignee="someone-else")

    with pytest.raises(SystemExit) as exc:
        task_outcome_cmd(slug, "cannot_proceed", detail="r", assignee=WORKER)

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Not recorded" in out
    assert "someone-else" in out
