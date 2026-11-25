"""The outcome a fan-out worker records on a task when its attempt ends.

A worker ends by recording exactly one outcome, and the runner and the review
queue act on it: ``ready_for_review`` (a branch, its commits and the output of
the check that proves the acceptance criteria), ``needs_human`` (the question)
or ``cannot_proceed`` (the reason). It is a structured field rather than a tag
plus a comment because agents have misread that kind of free-text signal.

A task carrying an outcome waits on a person: ``readiness.status_pickable``
keeps it out of ready work and ``task_claim`` refuses it until it is requeued
(``task_release``), which clears it. The server writes the fields through
``update_task`` (see ``server._update_task_once`` for when they are carried,
replaced or cleared).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, field_validator, model_validator


class OutcomeKind(StrEnum):
    READY_FOR_REVIEW = "ready_for_review"
    NEEDS_HUMAN = "needs_human"
    CANNOT_PROCEED = "cannot_proceed"


#: The Task properties an outcome occupies, in ``schema.pg`` order.
FIELDS = (
    "outcome",
    "outcome_detail",
    "outcome_branch",
    "outcome_commits",
    "outcome_check",
    "outcome_set_by",
    "outcome_set_at",
)

#: Every outcome field null: what a requeue, a new claim or a reopen writes.
CLEARED: dict[str, None] = dict.fromkeys(FIELDS)

#: What ``detail`` holds for each outcome, for error messages and the CLI.
DETAIL_LABEL = {
    OutcomeKind.READY_FOR_REVIEW: "summary",
    OutcomeKind.NEEDS_HUMAN: "question",
    OutcomeKind.CANNOT_PROCEED: "reason",
}


def _blank_to_none(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value


class TaskOutcome(BaseModel):
    """One recorded outcome, validated for what its kind requires.

    ``ready_for_review`` needs the branch, at least one commit and the check
    output, because a reviewer on another machine has nothing else to go on.
    ``needs_human`` needs the question and ``cannot_proceed`` the reason, both
    in ``detail``. Anything else is optional on every kind: a worker that
    stops to ask may already have pushed a branch the next session resumes
    from.
    """

    outcome: OutcomeKind
    detail: str | None = None
    branch: str | None = None
    commits: list[str] | None = None
    check_output: str | None = None

    @field_validator("detail", "check_output", "branch", mode="before")
    @classmethod
    def _blank_is_missing(cls, value: str | None) -> str | None:
        return _blank_to_none(value)

    @field_validator("branch")
    @classmethod
    def _strip_branch(cls, value: str | None) -> str | None:
        return value.strip() if value else value

    @field_validator("commits", mode="before")
    @classmethod
    def _drop_blank_commits(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        commits = [c.strip() for c in value if c and c.strip()]
        return commits or None

    @model_validator(mode="after")
    def _required_for_kind(self) -> TaskOutcome:
        missing = []
        if self.outcome is OutcomeKind.READY_FOR_REVIEW:
            missing = [
                name
                for name, value in (
                    ("branch", self.branch),
                    ("commits", self.commits),
                    ("check_output", self.check_output),
                )
                if not value
            ]
        elif not self.detail:
            missing = [f"detail (the {DETAIL_LABEL[self.outcome]})"]
        if missing:
            raise ValueError(
                f"outcome {self.outcome.value} requires {', '.join(missing)}"
            )
        return self

    def graph_fields(self, set_by: str, set_at: str) -> dict:
        """The Task properties this outcome writes, keyed as :data:`FIELDS`."""
        return {
            "outcome": self.outcome.value,
            "outcome_detail": self.detail,
            "outcome_branch": self.branch,
            "outcome_commits": self.commits,
            "outcome_check": self.check_output,
            "outcome_set_by": set_by,
            "outcome_set_at": set_at,
        }
