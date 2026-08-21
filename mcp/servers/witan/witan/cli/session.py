"""Session commands: start, end, list.

Sessions are the continuity primitive — ``workflow_session_start`` links an
agent session to a project and returns a handle, ``workflow_session_end`` takes
that handle back and records the handoff summary. The handle is persisted here,
client-side (``witan.session_state``), so the Stop hook can find it whether the
tool ran in-process or against a deployment. These were MCP-only; the CLI now
lets a human drive them too, e.g. to close a session that leaked open or to
inspect a project's session history.
"""

from __future__ import annotations

import os
import uuid

import cyclopts

from .. import session_state
from ._common import (
    _fn,
    _split_csv,
    _srv,
    app,
    console,
    esc,
    print_error,
    WorkflowPhase,
)

session_app = cyclopts.App(
    name="session",
    help="Manage workflow sessions.",
)
app.command(session_app)


@session_app.command(name="start")
def session_start(
    project_slug: str,
    *,
    phase: WorkflowPhase,
    session_id: str | None = None,
    repo: str | None = None,
    tags: list[str] | None = None,
) -> None:
    """Link a session to a workflow project.

    Parameters
    ----------
    project_slug: The ``wp-`` slug of the project this session belongs to.
    phase: The phase this session is working in.
    session_id: Unique id for the session (default: ``$CLAUDE_SESSION_ID`` or a
        generated uuid). The Stop hook keys its state file on this.
    repo: Repo URI to scope the session to (default: auto-detected).
    tags: Optional tags.
    """
    sid = session_id or os.environ.get("CLAUDE_SESSION_ID") or f"cli-{uuid.uuid4().hex}"
    s = _srv()
    result = _fn(s.workflow_session_start)(
        project_slug=project_slug,
        session_id=sid,
        phase=phase,
        repo=repo,
        tags=_split_csv(tags),
    )
    # Park the returned handle client-side. The server only writes it in local
    # stdio mode; doing it here is what makes the Stop hook work against a
    # deployment, where the replica that served this call shares no filesystem
    # with us. Re-writing the local-stdio file is harmless (same contents).
    session_state.write_handle(sid, dict(result))
    console.print(
        f"[green]Started session[/green] [bold]{result['session_slug']}[/bold]"
    )
    console.print(f"  project: {result['project_slug']}  phase: {result['phase']}")
    console.print(f"  session_id: {sid}")


@session_app.command(name="end")
def session_end(
    session_slug: str,
    *,
    summary: str,
    tools_used: list[str] | None = None,
    files_changed: list[str] | None = None,
) -> None:
    """Close a session with a handoff summary.

    Parameters
    ----------
    session_slug: The ``ws-`` slug returned by ``session start``.
    summary: What was accomplished and what remains — the resume artifact.
    tools_used: Optional list of tool names used.
    files_changed: Optional list of file paths modified.
    """
    s = _srv()
    result = _fn(s.workflow_session_end)(
        session_slug=session_slug,
        summary=summary,
        tools_used=_split_csv(tools_used),
        files_changed=_split_csv(files_changed),
    )
    # Drop our copy of the handle so the Stop hook doesn't re-close this session.
    session_state.clear_handle_for_slug(session_slug)
    console.print(f"[green]Ended session[/green] [bold]{session_slug}[/bold]")
    console.print(f"  ended_at: {result.get('ended_at')}")


_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_duration(spec: str) -> int:
    """``"6h"`` → 21600. Accepts s/m/h/d, or a bare number of seconds.

    Negative is rejected rather than clamped. A negative age puts the sweep's
    cutoff in the *future*, which makes every open session look stale — so
    ``--older-than -1h --yes``, a plausible typo, would close every session the
    caller can see instead of none.
    """
    text = spec.strip().lower()
    unit = _DURATION_UNITS.get(text[-1:], None)
    number = text[:-1] if unit else text
    try:
        value = float(number)
    except ValueError:
        raise ValueError(
            f"could not parse duration {spec!r} — use e.g. 30m, 6h, 2d"
        ) from None
    if value < 0:
        raise ValueError(f"duration {spec!r} cannot be negative")
    return int(value * (unit or 1))


@session_app.command(name="sweep")
def session_sweep(
    *,
    older_than: str = "6h",
    project: str | None = None,
    yes: bool = False,
) -> None:
    """Close sessions that leaked open.

    A session with no ``ended_at`` is not cosmetic: ``project complete`` folds
    every linked session into the corpus trace, so a leaked one inflates
    ``session_count``, contributes its phase having recorded nothing, carries no
    handoff summary, and cannot extend ``duration`` (computed from
    ``max(ended_at)``). It also drives the context hook's "N sessions in
    <phase>" staleness nag on a project whose phase is progressing fine.

    Dry-run by default — prints what it would close. Pass ``--yes`` to do it.
    Closing an already-closed session just re-stamps ``ended_at``, so re-running
    is harmless.

    Against a deployment the per-actor client scopes the listing to the calling
    user, so a sweep cannot reach a teammate's sessions.

    Parameters
    ----------
    older_than: Minimum age of a session to sweep (``30m``, ``6h``, ``2d``).
        Guards against closing a session that is legitimately running right now.
    project: Restrict to one project's sessions (``wp-`` slug).
    yes: Actually close them. Without this, nothing is written.
    """
    from datetime import datetime, timedelta, timezone

    try:
        max_age = timedelta(seconds=_parse_duration(older_than))
    except ValueError as exc:
        print_error(exc)
        raise SystemExit(1) from None

    s = _srv()
    sessions = _fn(s.workflow_session_list)(project_slug=project, open_only=True)
    cutoff = datetime.now(timezone.utc) - max_age

    stale, running = [], 0
    for sess in sessions:
        started = sess.get("started_at")
        # A session with no started_at can't be aged, and guessing would risk
        # closing a live one. Leave it and say so rather than sweeping blind.
        if not started:
            running += 1
            continue
        try:
            when = datetime.fromisoformat(started)
        except ValueError:
            running += 1
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when < cutoff:
            stale.append(sess)
        else:
            running += 1

    scope = f" in {project}" if project else ""
    if not stale:
        console.print(
            f"[dim]No sessions{scope} open longer than {older_than}"
            f"{f' ({running} newer, left alone)' if running else ''}.[/dim]"
        )
        return

    console.print(
        f"[bold]{len(stale)} session(s){scope} open longer than {older_than}[/bold]"
        + (f" ({running} newer, left alone)" if running else "")
        + (":" if yes else " — dry run, pass --yes to close:")
    )
    for sess in stale:
        console.print(
            f"  {sess['slug']}  ({sess.get('project_slug')}/{sess.get('phase')})"
            f"  started {sess.get('started_at')}"
        )

    if not yes:
        return

    summary = (
        f"Closed by `witan session sweep` — this session was left open for more "
        f"than {older_than} and recorded no handoff summary of its own. It was "
        f"not checkpointed, so nothing about what it did is known."
    )
    closed = 0
    for sess in stale:
        _fn(s.workflow_session_end)(session_slug=sess["slug"], summary=summary)
        # Drop our copy of the handle too, so a later Stop hook doesn't try to
        # re-close a session we just swept.
        session_state.clear_handle_for_slug(sess["slug"])
        closed += 1
    console.print(f"[green]Closed {closed} session(s).[/green]")


@session_app.command(name="list")
def session_list(project_slug: str) -> None:
    """List a project's sessions, newest last.

    Parameters
    ----------
    project_slug: The ``wp-`` slug whose sessions to list.
    """
    s = _srv()
    sessions = _fn(s.workflow_session_list)(
        project_slug=project_slug, include_superseded=True
    )
    if not sessions:
        console.print(f"[dim]No sessions for {project_slug}.[/dim]")
        return
    console.print(f"[bold]Sessions for {project_slug}[/bold] ({len(sessions)}):")
    for sess in sessions:
        # Unlike the aggregate views, this listing keeps superseded rows — it's
        # the view you reach for to see what `migrate dedupe-sessions` did.
        state = (
            "duplicate"
            if sess.get("superseded_by")
            else "open"
            if not sess.get("ended_at")
            else "ended"
        )
        summary = (sess.get("summary") or "(in progress)").splitlines()
        # Escape the free-text first line and use parentheses (not brackets) for
        # phase/state — Rich would parse "[implementation/open]" as a malformed
        # markup tag and could error out on it. Truncate before escaping, so a
        # cut never lands inside an escape sequence.
        first = esc((summary[0] if summary else "(in progress)")[:80])
        console.print(f"  {sess['slug']}  ({esc(sess.get('phase'))}/{state})  {first}")
