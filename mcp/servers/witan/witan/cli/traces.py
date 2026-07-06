"""Trace commands: list, show.

WorkflowTrace records are corpus material for two audiences: agents mining
for reusable patterns, and people onboarding onto the system who want a
worked example of how a project went end to end. ``trace show`` prints the
full outcome narrative and the mined lessons/patterns for that reason,
rather than just slugs.
"""

from __future__ import annotations

import cyclopts
from rich.table import Table

from ._common import (
    _detect_repo_for_display,
    _fn,
    _repo_arg,
    _short_repo,
    _srv,
    app,
    console,
)


@app.command
def traces(
    *,
    repo: str | None = None,
    tags: list[str] | None = None,
    author: str | None = None,
    all_repos: bool = False,
    limit: int = 50,
) -> None:
    """List corpus workflow traces (default: current repo)."""
    s = _srv()
    rows = _fn(s.workflow_trace_list)(
        repo=_repo_arg(repo, all_repos), tags=tags, author=author, limit=limit
    )
    if not rows:
        console.print("[dim]No traces.[/dim]")
        return
    detected_repo = (
        _detect_repo_for_display() if not all_repos and repo is None else repo
    )
    scope = (
        "all repos"
        if all_repos
        else (
            _short_repo(detected_repo)
            if detected_repo
            else "all repos (no git context)"
        )
    )
    table = Table(title=f"Workflow traces — {scope}", header_style="bold")
    for col in ("slug", "title", "sessions", "mined", "repos"):
        table.add_column(
            col, overflow="fold", no_wrap=col in {"slug", "sessions", "mined"}
        )
    for r in rows:
        mined = (
            f"{len(r.get('patterns_slug') or [])}p/{len(r.get('lessons_slug') or [])}l"
        )
        table.add_row(
            r["slug"],
            r.get("title", ""),
            str(r.get("session_count", "")),
            mined,
            ", ".join(_short_repo(u) for u in (r.get("repos") or [])),
        )
    console.print(table)


def _trace_show(slug: str) -> None:
    """Show a trace's outcome, sessions, and mined lessons/patterns."""
    s = _srv()
    rows = s.client.read("read.gq", "get_trace", {"slug": slug})
    if not rows:
        console.print(f"[red]No trace {slug!r}.[/red]")
        return
    tr = rows[0]

    console.print(f"[bold]{tr['slug']}[/bold]  {tr.get('title', '')}")
    console.print(
        f"  project={tr.get('project_slug')}  sessions={tr.get('session_count')}  "
        f"phases={', '.join(tr.get('phases') or [])}  duration={tr.get('duration')}h  "
        f"repos={', '.join(_short_repo(u) for u in (tr.get('repos') or [])) or '—'}"
    )
    console.print(f"\n{tr.get('description') or '(no description)'}\n")
    console.print(f"[bold]Outcome[/bold]\n{tr.get('outcome') or '(none recorded)'}\n")

    for label, key, kind in (
        ("Patterns mined", "patterns_slug", "pattern"),
        ("Lessons mined", "lessons_slug", "lesson"),
    ):
        slugs = tr.get(key) or []
        if not slugs:
            continue
        console.print(f"[bold]{label}[/bold]")
        for mslug in slugs:
            m = _fn(s.memory_get)(mslug)
            if m:
                console.print(f"  [cyan]{mslug}[/cyan]  {m.get('title', '')}")
                console.print(f"    {m.get('content', '')}"[:240])
            else:
                console.print(f"  [dim]{mslug} (missing)[/dim]")
        console.print()


trace_app = cyclopts.App(
    name="trace",
    help="Inspect corpus trace records.",
    default_command=_trace_show,
)
app.command(trace_app)


@trace_app.command(name="list")
def trace_list_cmd(
    *,
    repo: str | None = None,
    tags: list[str] | None = None,
    author: str | None = None,
    all_repos: bool = False,
    limit: int = 50,
) -> None:
    """List corpus workflow traces (alias of ``witan traces``)."""
    traces(repo=repo, tags=tags, author=author, all_repos=all_repos, limit=limit)
