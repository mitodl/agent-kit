"""The summary the Stop hook writes when it auto-closes a WorkflowSession.

Agents rarely call ``workflow_session_end``, so the auto-close is usually the
only close a session gets, and its summary is what the next session's handoff
(``workflow_project_status``'s ``last_session``) shows. A fixed placeholder told
that next session nothing. This builds the summary from what the graph and git
already recorded for the session instead: tasks this session claimed or closed,
memories it stored, commits on its branch, and uncommitted files.

Nothing here reads a harness transcript, because Claude Code, Pi and Codex
reach the Stop hook by different paths and keep different transcripts. What
they share is the parked handle (``session_slug``, ``project_slug``,
``started_at``) and the agent session id that qualifies a task claim's holder.

The reads themselves live in ``witan.cli.hooks``, which dispatches them through
``_srv()``; this module is the filtering and the rendering, plus the git reads,
so the shape of a summary is testable without a graph.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import readiness

AUTOCLOSE_PREFIX = "Session ended (auto-closed by Stop hook"

# The summary from before any of this existed, kept byte-identical because
# stored sessions carry it. Still written when every read failed, since then
# there is nothing true to say beyond it.
PLACEHOLDER = (
    f"{AUTOCLOSE_PREFIX} — call workflow_session_end explicitly for a better summary)"
)

# "In this project" because only the project's tasks are read (see
# `session_tasks`): a claim on a task filed outside it is not seen, and the
# line must not say it did not happen.
NO_ACTIVITY = (
    f"{AUTOCLOSE_PREFIX}) with no recorded activity: no tasks in this project "
    "claimed or closed, no memories stored, no commits, no uncommitted changes."
)

# Per list, so one busy session can't push the summary past what a handoff
# reader will look at. The overflow is counted, not dropped silently.
LIST_CAP = 10

GIT_TIMEOUT_SECONDS = 5

_IDENT_EMAIL = re.compile(r"<([^>]+)>")


@dataclass
class Activity:
    """What one session left behind. ``unread`` names each source whose read failed."""

    closed_tasks: list[dict] = field(default_factory=list)
    claimed_tasks: list[dict] = field(default_factory=list)
    memories: list[dict] = field(default_factory=list)
    branch: str | None = None
    commits: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.closed_tasks
            or self.claimed_tasks
            or self.memories
            or self.commits
            or self.changed_files
        )


def parse_timestamp(value: str | None) -> datetime | None:
    """An ISO timestamp as an aware UTC datetime, or ``None`` when it won't parse.

    The store hands ``DateTime`` columns back without an offset while
    ``now_iso()`` writes one, so a naive value is read as UTC, as the server's
    own window filters do.
    """
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _at_or_after(value: str | None, since: datetime) -> bool:
    moment = parse_timestamp(value)
    return moment is not None and moment >= since


def session_tasks(
    rows: list[dict], session_id: str, since: datetime
) -> tuple[list[dict], list[dict]]:
    """Split a project's task rows into (closed, claimed) by this session.

    A task is this session's when its holder carries this session's
    ``#<suffix>`` qualifier. The person half of the holder is not compared:
    against a deployment it is the token's ``preferred_username``, which the
    hook cannot know, and the suffix alone already names one session. Closing
    keeps the holder, so a closed task is still attributable; a released one
    is not, and is left out.

    ``since`` matters because one agent session id can span several
    WorkflowSessions, each a separate working stint, and only this stint's
    claims and closes belong in its summary.

    The rows are the project's tasks only. Reading by holder across every
    task would also catch claims on tasks filed outside the project, but
    ``task_list`` has no holder index, so that is a scan of the whole store on
    every Stop.
    """
    suffix = readiness.session_suffix(session_id)
    if not suffix:
        return [], []
    mine = [r for r in rows if (r.get("assignee") or "").endswith(f"#{suffix}")]
    closed = [
        r
        for r in mine
        if r.get("status") == "closed" and _at_or_after(r.get("closed_at"), since)
    ]
    claimed = [
        r
        for r in mine
        if r.get("status") != "closed" and _at_or_after(r.get("claimed_at"), since)
    ]
    return closed, claimed


def _git(project_dir: str, *args: str) -> list[str]:
    return subprocess.check_output(
        ["git", "-C", project_dir, *args],
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=GIT_TIMEOUT_SECONDS,
    ).splitlines()


def changed_files(project_dir: str) -> list[str]:
    """Files dirty in the agent's checkout, or ``[]`` outside a repo."""
    try:
        return _git(project_dir, "diff", "--name-only", "HEAD")[:50]
    except (subprocess.SubprocessError, OSError):
        return []


def current_branch(project_dir: str) -> str | None:
    """The checked-out branch, or ``None`` on a detached HEAD or outside a repo."""
    try:
        lines = _git(project_dir, "rev-parse", "--abbrev-ref", "HEAD")
    except (subprocess.SubprocessError, OSError):
        return None
    branch = lines[0] if lines else ""
    return branch if branch and branch != "HEAD" else None


def session_commits(project_dir: str, since: datetime) -> list[str]:
    """``<short sha> <subject>`` for each commit made on HEAD since ``since``.

    Limited to the checkout's git author, so commits a ``git pull`` brought in
    from other people during the window are not credited to this session.
    Raises when git fails, so the caller can report the source as unread
    rather than as "no commits".
    """
    args = [
        "log",
        "--no-merges",
        f"--since={since.isoformat()}",
        f"-n{LIST_CAP + 1}",
        "--format=%h %s",
    ]
    email = _author_email(project_dir)
    if email:
        args.append(f"--author={email}")
    return _git(project_dir, *args, "HEAD")


def _author_email(project_dir: str) -> str | None:
    """The email ``git commit`` would record here, or ``None`` when there is none.

    ``git var`` rather than ``git config user.email`` so ``$GIT_AUTHOR_EMAIL``
    and ``$EMAIL`` count the way they do for the commit itself.
    """
    try:
        ident = _git(project_dir, "var", "GIT_AUTHOR_IDENT")
    except subprocess.CalledProcessError:  # no identity configured at all
        return None
    match = _IDENT_EMAIL.search(ident[0]) if ident else None
    return match.group(1) if match else None


def git_activity(activity: Activity, project_dir: str, since: datetime) -> None:
    """Fill in the branch, commits and uncommitted files from ``project_dir``.

    A directory that is not a checkout has no git activity to report, which is
    not a failed read: that session's summary can still say "no activity".
    """
    try:
        _git(project_dir, "rev-parse", "--git-dir")
    except (subprocess.SubprocessError, OSError):
        return
    activity.changed_files = changed_files(project_dir)
    activity.branch = current_branch(project_dir)
    try:
        activity.commits = session_commits(project_dir, since)
    except (subprocess.SubprocessError, OSError):
        activity.unread.append("commits")


def _capped(items: list[str]) -> str:
    shown = "; ".join(items[:LIST_CAP])
    extra = len(items) - LIST_CAP
    return f"{shown}; and {extra} more" if extra > 0 else shown


def _task_label(row: dict) -> str:
    return f"{row['slug']} ({row.get('title') or 'untitled'})"


def render(activity: Activity) -> str:
    """The summary text for ``activity``.

    Every variant starts with :data:`AUTOCLOSE_PREFIX`, which is how the server
    tells an auto-close from a summary someone wrote (``_has_own_summary``).
    No activity and no failed read gives :data:`NO_ACTIVITY`, an honest short
    line. No activity *with* a failed read gives :data:`PLACEHOLDER`, because
    "nothing happened" would then be a claim the hook couldn't check.
    """
    if activity.is_empty():
        return PLACEHOLDER if activity.unread else NO_ACTIVITY

    lines = [
        f"{AUTOCLOSE_PREFIX}; derived from recorded activity, not written by the agent)."
    ]
    if activity.closed_tasks:
        lines.append(
            "Closed tasks: " + _capped([_task_label(r) for r in activity.closed_tasks])
        )
    if activity.claimed_tasks:
        lines.append(
            "Claimed tasks: "
            + _capped(
                [
                    f"{_task_label(r)} [{r.get('status')}]"
                    for r in activity.claimed_tasks
                ]
            )
        )
    if activity.memories:
        lines.append(
            "Memories stored: "
            + _capped(
                [f"{m['slug']} ({m.get('title') or ''})" for m in activity.memories]
            )
        )
    if activity.branch:
        lines.append(f"Branch: {activity.branch}")
    if activity.commits:
        lines.append("Commits: " + _capped(activity.commits))
    if activity.changed_files:
        n = len(activity.changed_files)
        lines.append(
            f"Uncommitted changes in {n} file{'s' if n != 1 else ''}: "
            + _capped(activity.changed_files)
        )
    if activity.unread:
        lines.append(f"Could not read: {', '.join(activity.unread)}.")
    return "\n".join(lines)
