"""Project commands: list, show, status, create, run."""

from __future__ import annotations

import json as _json
from typing import Annotated

import cyclopts
from rich.markup import escape
from rich.prompt import Prompt

from .. import config as cfg_module
from ._common import (
    _STATUS_STYLE,
    _detect_repo_for_display,
    _fn,
    _repo_arg,
    _short_repo,
    _split_csv,
    _srv,
    _styled,
    WorkflowPhase,
    app,
    console,
    render_table,
)
from .run_helpers import (
    _launch_agent,
    _merge_prompts,
    _pick_items,
    _project_run_prompt,
    _run_project_slug,
)


@app.command
def projects(
    *,
    repo: str | None = None,
    status: str | None = "active",
    all_repos: bool = False,
    limit: int = 50,
) -> None:
    """List workflow projects (default: active in the current repo)."""
    s = _srv()
    rows = _fn(s.workflow_project_list)(repo=_repo_arg(repo, all_repos), status=status)[
        :limit
    ]
    if not rows:
        console.print("[dim]No projects.[/dim]")
        return
    detected_repo = (
        _detect_repo_for_display() if not all_repos and repo is None else repo
    )
    if all_repos:
        scope = "all repos"
    elif detected_repo:
        scope = _short_repo(detected_repo)
    else:
        scope = "all repos (no git context)"
    rows_data = [
        {
            "status": r.get("status", ""),
            "phase": r.get("phase", ""),
            "slug": r["slug"],
            "title": r.get("title", ""),
            "repos": ", ".join(_short_repo(u) for u in (r.get("repos") or [])),
        }
        for r in rows
    ]
    render_table(
        title=f"Workflow projects — {scope}",
        columns=["status", "phase", "slug", "title", "repos"],
        rows=rows_data,
        no_wrap={"status", "phase"},
        styles={"status": _STATUS_STYLE},
    )


def _project_show(slug: str) -> None:
    """Show a project, its sessions, and rolled-up tasks."""
    s = _srv()
    p = _fn(s.workflow_project_get)(slug)
    if not p:
        console.print(f"[red]No project {slug!r}.[/red]")
        return
    console.print(f"[bold]{p['slug']}[/bold]  {p.get('title', '')}")
    console.print(
        f"  status={_styled(p.get('status', ''), _STATUS_STYLE)}  "
        f"phase={p.get('phase')}  repos={', '.join(p.get('repos') or []) or '—'}"
    )
    if p.get("github_issue"):
        console.print(f"  issue: {p['github_issue']}")
    if p.get("github_pr"):
        console.print(f"  pr: {p['github_pr']}")
    if p.get("blocked_by"):
        for blocker in p["blocked_by"]:
            rows = s.client.read(
                "read.gq", "get_workflow_project_by_slug", {"slug": blocker}
            )
            b = rows[0] if rows else None
            st = b.get("status") if b else "missing"
            console.print(f"  blocked by {blocker} [{_styled(st, _STATUS_STYLE)}]")
    if p.get("blocks"):
        console.print(f"  blocks: {', '.join(p['blocks'])}")
    console.print(f"\n{p.get('description') or '(no description)'}\n")

    sessions = [
        sess
        for sess in s.client.read(
            "read.gq", "list_sessions_by_project", {"project_slug": slug}
        )
        if not sess.get("superseded_by")
    ]
    console.print(f"  sessions: {len(sessions)}")
    for sess in sessions:
        console.print(
            f"    {sess['slug']}  [{sess.get('phase')}]  "
            f"{sess.get('summary') or '(in progress)'}"[:120]
        )

    project_tasks = _fn(s.task_list)(project_slug=slug)
    if project_tasks:
        console.print(f"  tasks: {len(project_tasks)}")
        for t in project_tasks:
            console.print(
                f"    {t['slug']} [{_styled(t.get('status', ''), _STATUS_STYLE)}] {t.get('title', '')}"
            )

    if p.get("status") == "completed":
        trace = s.client.read("read.gq", "get_trace", {"slug": f"wt-{slug}"})
        if trace:
            tr = trace[0]
            console.print(
                f"\n  [blue]trace[/blue]: {tr.get('session_count')} sessions, "
                f"phases={tr.get('phases')}, duration={tr.get('duration')}h"
            )
            if tr.get("outcome"):
                console.print(f"    outcome: {tr['outcome']}"[:200])
            console.print(
                f"    lessons: {', '.join(tr.get('lessons_slug') or []) or '(none mined yet)'}"
            )
            console.print(
                f"    patterns: {', '.join(tr.get('patterns_slug') or []) or '(none mined yet)'}"
            )


project_app = cyclopts.App(
    name="project",
    help="Manage workflow projects.",
    default_command=_project_show,
)
app.command(project_app)


@project_app.command(name="status")
def project_status(
    slug: str,
    *,
    json: Annotated[bool, cyclopts.Parameter(name="--json")] = False,
) -> None:
    """Resume view — phase, ready tasks, last session, blockers ("what next").

    The single-call resume view for a project. Pass ``--json`` for the raw
    ``workflow_project_status`` payload.

    Parameters
    ----------
    slug: Project ``wp-`` slug.
    json: Emit the raw JSON payload instead of the formatted view.
    """
    s = _srv()
    st = _fn(s.workflow_project_status)(slug)
    if not st:
        console.print(f"[red]No project {slug!r}.[/red]")
        raise SystemExit(1)

    if json:
        console.print_json(_json.dumps(st))
        return

    p = st["project"]
    repos_s = ", ".join(_short_repo(r) for r in (p.get("repos") or [])) or "—"
    console.print(f"[bold]{p['slug']}[/bold]  {escape(p.get('title', ''))}")
    console.print(
        f"  phase={p.get('phase')}  "
        f"status={_styled(p.get('status', ''), _STATUS_STYLE)}  repos={repos_s}"
    )
    if p.get("github_pr"):
        console.print(f"  pr: {p['github_pr']}")
    if st["blockers"]:
        console.print(f"  [yellow]blocked by[/yellow]: {', '.join(st['blockers'])}")

    ls = st["last_session"]
    if ls:
        state = "still open" if ls["open"] else f"ended {ls['ended_at']}"
        # Free-text summary: collapse whitespace (multi-line summaries) and
        # truncate, then escape — so a literal "[bug]" isn't parsed as Rich
        # markup and slicing can't strip a closing tag.
        summary = escape(" ".join((ls.get("summary") or "(no summary)").split())[:250])
        console.print(f"\n  [blue]last session[/blue] ({state}): {summary}")
    else:
        console.print("\n  [dim]no sessions yet[/dim]")

    c = st["counts"]
    console.print(
        f"\n  [bold]ready tasks[/bold]: {c['ready']} of {c['open_tasks']} open"
    )
    for t in st["ready_tasks"]:
        held = f" [dim](claimed by {t['assignee']})[/dim]" if t.get("assignee") else ""
        # Escape the priority brackets so Rich renders literal "[p1]"; escape the
        # free-text title so a "[bug]"-style title isn't swallowed as markup.
        console.print(
            f"    \\[{t.get('priority', 'p2')}] {t['slug']}  "
            f"{escape(t.get('title', ''))}{held}"
        )


@project_app.command(name="tasks")
def project_tasks(
    slug: str,
    *,
    status: str | None = None,
    detail: bool = False,
) -> None:
    """List a project's tasks, optionally with their dependency structure.

    ``project <slug>`` already shows a flat task list; this focuses on the tasks
    and, with ``--detail``, expands each task's blockers (what it waits on) and
    dependents (what waits on it), resolving statuses from the project's own task
    set so the dependency chain is visible without hopping between commands.

    Parameters
    ----------
    slug: Project ``wp-`` slug.
    status: Filter to open | in_progress | blocked | closed.
    detail: Expand each task's blockers and dependents.
    """
    s = _srv()
    p = _fn(s.workflow_project_get)(slug)
    if not p:
        console.print(f"[red]No project {slug!r}.[/red]")
        raise SystemExit(1)

    rows = _fn(s.task_list)(project_slug=slug)
    if status:
        rows = [r for r in rows if r.get("status") == status]
    if not rows:
        scope = f" ({status})" if status else ""
        console.print(f"[dim]No tasks{scope} for {slug}.[/dim]")
        return

    scope = f" ({status})" if status else ""
    console.print(
        f"[bold]Tasks{scope} — {escape(p.get('title', slug))}[/bold] ({len(rows)}):"
    )
    for r in rows:
        blk = r.get("blocked_by") or []
        blk_note = f" [dim](blocked by {len(blk)})[/dim]" if blk else ""
        console.print(
            f"  \\[{r.get('priority', 'p2')}] "
            f"[{_styled(r.get('status', ''), _STATUS_STYLE)}] "
            f"{r['slug']}  {escape(r.get('title', ''))}{blk_note}"
        )

    if not detail:
        return

    # Dependents = tasks in this project that name r as a blocker. Resolve
    # statuses from the project's own set first, falling back to a fetch for a
    # cross-project/unscoped blocker referenced from here.
    by_slug = {r["slug"]: r for r in rows}
    dependents: dict[str, list[str]] = {}
    for r in rows:
        for b in r.get("blocked_by") or []:
            dependents.setdefault(b, []).append(r["slug"])

    def _status_of(task_slug: str) -> str:
        if task_slug in by_slug:
            return by_slug[task_slug].get("status") or "open"
        fetched = _fn(s.task_get)(task_slug)
        return fetched.get("status", "missing") if fetched else "missing"

    console.print("\n[bold]Dependencies[/bold]")
    any_edges = False
    for r in rows:
        blockers = r.get("blocked_by") or []
        deps = dependents.get(r["slug"], [])
        if not blockers and not deps:
            continue
        any_edges = True
        console.print(f"  [bold]{r['slug']}[/bold] {escape(r.get('title', ''))}")
        for b in blockers:
            console.print(
                f"    ↑ blocked by {b} [{_styled(_status_of(b), _STATUS_STYLE)}]"
            )
        for d in deps:
            console.print(f"    ↓ blocks {d} [{_styled(_status_of(d), _STATUS_STYLE)}]")
    if not any_edges:
        console.print("  [dim]no dependencies between these tasks.[/dim]")


@project_app.command(name="create")
def project_create(
    title: str,
    *,
    description: str = "",
    phase: WorkflowPhase = "discovery",
    repo: str | None = None,
    github_issue: str | None = None,
    tags: list[str] | None = None,
) -> None:
    """Create a new workflow project.

    Parameters
    ----------
    title: Short name for the project.
    description: Full objective description — what will be built and why.
    phase: Starting phase: discovery | spec | implementation | delivery.
    repo: Repo URI to associate (default: auto-detected from git remote).
    github_issue: URL of the tracking GitHub issue.
    tags: Optional tags for grouping and search.
    """
    s = _srv()
    repos = [repo] if repo else None
    result = _fn(s.workflow_project_create)(
        title=title,
        description=description,
        phase=phase,
        repos=repos,
        github_issue=github_issue,
        tags=_split_csv(tags),
    )
    console.print(f"[green]Created project:[/green] [bold]{result['slug']}[/bold]")
    if result.get("repos"):
        console.print(f"  repos: {', '.join(_short_repo(r) for r in result['repos'])}")
    console.print(f"  phase: {result['phase']}")


@project_app.command(name="advance")
def project_advance(
    slug: str,
    *,
    phase: WorkflowPhase,
    github_pr: str | None = None,
) -> None:
    """Advance a project to a new phase.

    A backward or skip transition is not blocked from the CLI (elicitation is
    only available in an MCP session), but the resulting ``advisory`` note is
    surfaced so an unusual transition is still visible.

    Parameters
    ----------
    slug: Project ``wp-`` slug.
    phase: New phase: discovery | spec | implementation | delivery.
    github_pr: URL of the PR, if one has been opened (recorded on delivery).
    """
    s = _srv()
    result = _fn(s.workflow_project_advance)(
        slug=slug, phase=phase, github_pr=github_pr
    )
    # Escape the advisory: it is free text and could carry bracketed fragments
    # Rich would try to parse as markup.
    advisory = escape((result.get("advisory") or "").strip())
    if result.get("advanced") is False:
        console.print(f"[yellow]Not advanced:[/yellow] {advisory}")
        return
    console.print(
        f"[green]Advanced[/green] [bold]{slug}[/bold] → phase "
        f"[bold]{result.get('phase', phase)}[/bold]"
    )
    if advisory:
        console.print(f"  [yellow]note:[/yellow] {advisory}")


@project_app.command(name="complete")
def project_complete(
    slug: str,
    *,
    outcome: str,
    github_pr: str | None = None,
) -> None:
    """Complete a project and seal its immutable corpus trace.

    Parameters
    ----------
    slug: Project ``wp-`` slug.
    outcome: Narrative of what was delivered — the primary content of the trace.
    github_pr: URL of the merged PR, if applicable.
    """
    s = _srv()
    result = _fn(s.workflow_project_complete)(
        slug=slug, outcome=outcome, github_pr=github_pr
    )
    if result.get("existed"):
        console.print(
            f"[dim]Trace {result.get('trace_slug')} already exists — no change.[/dim]"
        )
        return
    console.print(f"[green]Completed[/green] [bold]{slug}[/bold]")
    if result.get("trace_slug"):
        console.print(f"  trace: {result['trace_slug']}")


@project_app.command(name="block")
def project_block(slug: str, blocks: str) -> None:
    """Declare that ``slug`` must complete before ``blocks`` can begin.

    Parameters
    ----------
    slug: The blocking project's ``wp-`` slug (must finish first).
    blocks: The blocked project's ``wp-`` slug.
    """
    s = _srv()
    result = _fn(s.workflow_project_block)(slug=slug, blocks_slug=blocks)
    if not result.get("linked"):
        reason = escape((result.get("reason") or "").strip() or "unknown")
        console.print(f"[red]Not linked:[/red] {reason}")
        return
    console.print(f"[green]Blocked[/green] {blocks} on {slug}")


@project_app.command(name="unblock")
def project_unblock(slug: str, blocks: str) -> None:
    """Remove a project dependency declared with ``project block``.

    Parameters
    ----------
    slug: The blocking project's ``wp-`` slug to remove.
    blocks: The project to unblock.
    """
    s = _srv()
    result = _fn(s.workflow_project_unblock)(slug=slug, blocks_slug=blocks)
    if result.get("removed"):
        console.print(f"[green]Unblocked[/green] {blocks} (removed blocker {slug})")
    else:
        console.print(f"[dim]{slug} was not a blocker of {blocks} — no change.[/dim]")


@project_app.command(name="run")
def project_run(
    slug: str | None = None,
    *,
    target: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    dry_run: bool = False,
    repo: str | None = None,
    all_repos: bool = False,
) -> None:
    """Launch an agent session focused on a workflow project.

    Without a slug, shows an interactive picker of active projects. Multiple
    selections offer a choice between a consolidated single-session prompt or
    running each project sequentially in separate agent invocations.

    Parameters
    ----------
    slug: Project slug to run directly (skips the picker).
    target: Named config target (overrides auto-detection).
    agent: Agent CLI to launch (claude, pi, copilot, opencode, kilo).
    model: Model flag passed to the agent.
    dry_run: Print the prompt(s) without launching.
    repo: Scope the picker to a specific repo URI.
    all_repos: Span all repos in the picker.
    """
    try:
        cfg = cfg_module.load(target=target)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None

    resolved_agent = agent or cfg.agent
    resolved_model = model or cfg.model

    s = _srv()

    if slug:
        _run_project_slug(slug, cfg=cfg, agent=agent, model=model, dry_run=dry_run)
        return

    repo_arg = _repo_arg(repo, all_repos)
    active = _fn(s.workflow_project_list)(repo=repo_arg, status="active")[:50]
    if not active:
        console.print("[dim]No active projects.[/dim]")
        return

    console.print(f"[bold]Active projects[/bold] ({len(active)} available):\n")

    def _render_project(p: dict) -> str:
        phase = f"[dim]{p.get('phase')}[/dim]"
        repos_s = (
            f"  [dim]{', '.join(_short_repo(r) for r in (p.get('repos') or []))}[/dim]"
        )
        return f"{p['slug']}  {phase}  {p.get('title', '')}{repos_s}"

    selected = _pick_items(active, _render_project)
    if not selected:
        console.print("[dim]Nothing selected.[/dim]")
        return

    if len(selected) == 1:
        _run_project_slug(
            selected[0]["slug"], cfg=cfg, agent=agent, model=model, dry_run=dry_run
        )
        return

    console.print(f"\n[bold]{len(selected)} projects selected.[/bold]")
    console.print("  [1] Consolidate: one agent session covering all projects")
    console.print("  [2] Sequential: a separate agent invocation per project")
    try:
        choice = Prompt.ask("Choice", choices=["1", "2"], default="1", console=console)
    except (EOFError, KeyboardInterrupt):
        console.print("\n[yellow]Cancelled.[/yellow]")
        raise SystemExit(0)
    mode = "c" if choice == "1" else "s"

    if mode == "c":
        prompts = []
        for p in selected:
            p_tasks = _fn(s.task_ready)(project_slug=p["slug"], limit=20)
            prompts.append(_project_run_prompt(p, p_tasks))
        merged = _merge_prompts(prompts, "project")
        _launch_agent(cfg, resolved_agent, resolved_model, merged, dry_run)
    else:
        for p in selected:
            console.print(f"\n[bold]── {p['slug']}: {p.get('title', '')} ──[/bold]")
            _run_project_slug(
                p["slug"], cfg=cfg, agent=agent, model=model, dry_run=dry_run
            )
