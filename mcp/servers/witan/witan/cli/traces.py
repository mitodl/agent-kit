"""Trace commands: list, show, annotate, mine.

WorkflowTrace records are corpus material for two audiences: agents mining
for reusable patterns, and people onboarding onto the system who want a
worked example of how a project went end to end. ``trace show`` prints the
full outcome narrative and the mined lessons/patterns for that reason,
rather than just slugs.

These commands are the supported route to the ``workflow_trace_*`` tools: the
server lists those tools to witan's own CLI and to no other client
(``witan.cli_only``).
"""

from __future__ import annotations

import json
from pathlib import Path

import cyclopts

from ._common import (
    _detect_repo_for_display,
    _fn,
    _repo_arg,
    _short_repo,
    _srv,
    app,
    console,
    esc,
    print_error,
    render_table,
)
from .output import dump_record, get_output_format


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
        tags=tags, author=author, limit=limit, **_repo_arg(repo, all_repos)
    )
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
    rows_data = [
        {
            "slug": r["slug"],
            "title": r.get("title", ""),
            "sessions": r.get("session_count"),
            "mined": (
                f"{len(r.get('patterns_slug') or [])}p/"
                f"{len(r.get('lessons_slug') or [])}l"
            ),
            "repos": ", ".join(_short_repo(u) for u in (r.get("repos") or [])),
        }
        for r in rows
    ]
    render_table(
        title=f"Workflow traces — {scope}",
        columns=["slug", "title", "sessions", "mined", "repos"],
        rows=rows_data,
        empty="[dim]No traces.[/dim]",
        no_wrap={"slug", "sessions", "mined"},
        records=rows,
    )


def _trace_show(slug: str) -> None:
    """Show a trace's outcome, sessions, and mined lessons/patterns."""
    s = _srv()
    tr = _fn(s.workflow_trace_get)(slug=slug)
    if not tr:
        print_error(f"No trace {slug!r}.")
        return

    console.print(f"[bold]{tr['slug']}[/bold]  {esc(tr.get('title'))}")
    console.print(
        f"  project={tr.get('project_slug')}  sessions={tr.get('session_count')}  "
        f"phases={esc(', '.join(tr.get('phases') or []))}  "
        f"duration={tr.get('duration')}h  "
        f"repos={', '.join(_short_repo(u) for u in (tr.get('repos') or [])) or '—'}"
    )
    console.print(f"\n{esc(tr.get('description') or '(no description)')}\n")
    console.print(
        f"[bold]Outcome[/bold]\n{esc(tr.get('outcome') or '(none recorded)')}\n"
    )

    sessions = _fn(s.workflow_session_list)(project_slug=tr.get("project_slug"))
    if sessions:
        console.print("[bold]Sessions[/bold]")
        for sess in sessions:
            summary = (sess.get("summary") or "(in progress)")[:80]
            console.print(
                f"  {sess['slug']}  ({esc(sess.get('phase'))})  {esc(summary)}"
            )
        console.print()

    for label, key in (
        ("Patterns mined", "patterns_slug"),
        ("Lessons mined", "lessons_slug"),
    ):
        slugs = tr.get(key) or []
        if not slugs:
            continue
        console.print(f"[bold]{label}[/bold]")
        for mslug in slugs:
            m = _fn(s.memory_get)(slug=mslug)
            if m:
                console.print(f"  [cyan]{mslug}[/cyan]  {esc(m.get('title'))}")
                console.print(f"    {esc((m.get('content') or '')[:236])}")
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


def _trace_slug(slug: str) -> str:
    """Accept the project slug too, as ``witan trace <slug>`` does."""
    return slug if slug.startswith("wt-") else f"wt-{slug}"


@trace_app.command(name="annotate")
def trace_annotate_cmd(
    slug: str,
    *,
    lesson: list[str] | None = None,
    pattern: list[str] | None = None,
) -> None:
    """Record lesson/pattern memory slugs a completed trace produced.

    Unions with what the trace already records, so re-running is safe.

    Parameters
    ----------
    slug:
        The ``wt-`` trace slug, or the ``wp-`` project slug it was built from.
    lesson:
        A ``les-`` memory slug to add. Repeat for more than one.
    pattern:
        A ``pat-`` memory slug to add. Repeat for more than one.
    """
    if not lesson and not pattern:
        print_error("Pass at least one --lesson or --pattern slug.")
        raise SystemExit(1)
    result = _fn(_srv().workflow_trace_annotate)(
        trace_slug=_trace_slug(slug), lessons_slug=lesson, patterns_slug=pattern
    )
    if result.get("error"):
        print_error(f"No trace {result['slug']!r}.")
        raise SystemExit(1)
    fmt = get_output_format()
    if fmt != "txt":
        dump_record(result, fmt)
        return
    console.print(
        f"Annotated {result['slug']}: "
        f"{len(result.get('lessons_slug') or [])} lesson(s), "
        f"{len(result.get('patterns_slug') or [])} pattern(s) recorded."
    )


def _load_proposals(path: Path) -> tuple[list, list]:
    """Read a ``--proposals`` file, refusing one that would create nothing.

    The server reads any ``patterns``/``lessons`` argument, even an empty one,
    as the write call, so a typo'd key or two empty lists would exit 0 having
    created nothing.

    Each proposal with neither ``repo`` nor ``all_repos`` gets the caller's
    detected repo. A deployment has no checkout to detect one from, and the
    remote proxy fills only a tool's top-level ``repo``, not one nested in a
    proposal. One with ``all_repos`` is left alone, because the server refuses
    ``repo`` beside it.
    """
    spec = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict):
        print_error(
            f"{path} must hold a JSON object with 'patterns' and/or 'lessons' "
            f"lists, not a JSON {type(spec).__name__}."
        )
        raise SystemExit(1)
    groups = []
    for key in ("patterns", "lessons"):
        items = spec.get(key) or []
        groups.append([items] if isinstance(items, dict) else items)
    patterns, lessons = groups
    if not patterns and not lessons:
        print_error(
            f"{path} proposes no patterns or lessons (keys found: "
            f"{', '.join(spec) or 'none'}). Expected non-empty 'patterns' "
            "and/or 'lessons' lists."
        )
        raise SystemExit(1)
    repo = _detect_repo_for_display()
    if repo is not None:
        for item in (*patterns, *lessons):
            if (
                isinstance(item, dict)
                and item.get("repo") is None
                and not item.get("all_repos")
            ):
                item["repo"] = repo
    return patterns, lessons


@trace_app.command(name="mine")
def trace_mine_cmd(
    slug: str,
    *,
    proposals: Path | None = None,
    session_slug: str | None = None,
) -> None:
    """Turn a completed trace into reusable pattern and lesson memories.

    Run without ``--proposals`` first to print the raw material: the trace
    and every session summary from its project. Review it, write the patterns
    and lessons worth keeping to a JSON file, and run again with
    ``--proposals`` to create them. Each becomes a memory linked back to the
    trace's project and recorded on the trace.

    Parameters
    ----------
    slug:
        The ``wt-`` trace slug, or the ``wp-`` project slug it was built from.
    proposals:
        JSON file of the form ``{"patterns": [...], "lessons": [...]}``. Each
        entry needs ``title`` and ``content``. A pattern may add ``repo``,
        ``language`` and ``tags``; a lesson may add ``repo``, ``severity`` and
        ``tags``. Either may set ``all_repos`` to file it under no repo
        instead of the current one.
    session_slug:
        The ``ws-`` session to record as the provenance of every mined memory.
    """
    trace_slug = _trace_slug(slug)
    kwargs: dict = {"trace_slug": trace_slug, "session_slug": session_slug}
    if proposals is not None:
        kwargs["patterns"], kwargs["lessons"] = _load_proposals(proposals)
    result = _fn(_srv().workflow_trace_mine)(**kwargs)
    if result.get("error"):
        print_error(f"No trace {trace_slug!r}.")
        raise SystemExit(1)

    fmt = get_output_format()
    if fmt != "txt":
        dump_record(result, fmt)
        return

    if proposals is not None:
        for label, key in (
            ("pattern", "created_patterns"),
            ("lesson", "created_lessons"),
        ):
            for created in result[key]:
                console.print(f"Created {label} [cyan]{created}[/cyan]")
        if result.get("redaction_note"):
            console.print(f"[yellow]{esc(result['redaction_note'])}[/yellow]")
        return

    tr = result["trace"]
    console.print(f"[bold]{tr['slug']}[/bold]  {esc(tr.get('title'))}")
    console.print(f"\n{esc(tr.get('description') or '(no description)')}\n")
    console.print(
        f"[bold]Outcome[/bold]\n{esc(tr.get('outcome') or '(none recorded)')}\n"
    )
    for sess in result["sessions"]:
        console.print(f"[bold]{sess['slug']}[/bold]  ({esc(sess.get('phase'))})")
        console.print(f"{esc(sess.get('summary') or '(no summary)')}\n")
