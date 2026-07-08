"""Project commands: list, show, status, create, run."""

from __future__ import annotations

import json as _json
from typing import Annotated

import cyclopts
from rich.markup import escape
from rich.table import Table

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
    table = Table(title=f"Workflow projects — {scope}", header_style="bold")
    _short_cols = {"status", "phase"}
    for col in ("status", "phase", "slug", "title", "repos"):
        if col in _short_cols:
            table.add_column(col, no_wrap=True)
        else:
            table.add_column(col, overflow="fold", no_wrap=False)
    for r in rows:
        table.add_row(
            _styled(r.get("status", ""), _STATUS_STYLE),
            r.get("phase", ""),
            r["slug"],
            r.get("title", ""),
            ", ".join(_short_repo(u) for u in (r.get("repos") or [])),
        )
    console.print(table)


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

    sessions = s.client.read(
        "read.gq", "list_sessions_by_project", {"project_slug": slug}
    )
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
    console.print("  [c] Consolidate into one agent session")
    console.print("  [s] Run sequentially (one agent per project)")
    try:
        mode = input("Choice [c/s]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print("\n[yellow]Cancelled.[/yellow]")
        raise SystemExit(0)

    if mode == "c":
        prompts = []
        for p in selected:
            p_tasks = _fn(s.task_ready)(project_slug=p["slug"], limit=20)
            prompts.append(_project_run_prompt(p, p_tasks))
        merged = _merge_prompts(prompts, "project")
        _launch_agent(cfg, resolved_agent, resolved_model, merged, dry_run)
    elif mode == "s":
        for p in selected:
            console.print(f"\n[bold]── {p['slug']}: {p.get('title', '')} ──[/bold]")
            _run_project_slug(
                p["slug"], cfg=cfg, agent=agent, model=model, dry_run=dry_run
            )
    else:
        console.print("[red]Invalid choice. Aborting.[/red]")
        raise SystemExit(1)
