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
from rich.markup import escape

from .. import session_state
from ._common import (
    _fn,
    _split_csv,
    _srv,
    WorkflowPhase,
    app,
    console,
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


@session_app.command(name="list")
def session_list(project_slug: str) -> None:
    """List a project's sessions, newest last.

    Parameters
    ----------
    project_slug: The ``wp-`` slug whose sessions to list.
    """
    s = _srv()
    sessions = s.client.read(
        "read.gq", "list_sessions_by_project", {"project_slug": project_slug}
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
        # markup tag and could error out on it.
        first = escape(summary[0] if summary else "(in progress)")
        console.print(f"  {sess['slug']}  ({sess.get('phase')}/{state})  {first}"[:140])
