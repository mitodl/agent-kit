"""Task commands: list, show, create, run."""

from __future__ import annotations

import cyclopts
from rich.prompt import Prompt
from rich.table import Table

from .. import config as cfg_module
from ._common import (
    _PRIORITY_STYLE,
    _STATUS_STYLE,
    _detect_repo_for_display,
    _fn,
    _repo_arg,
    _short_repo,
    _split_csv,
    _srv,
    _styled,
    TaskPriority,
    TaskType,
    app,
    console,
)
from .run_helpers import (
    _launch_agent,
    _merge_prompts,
    _pick_items,
    _run_prompt,
    _run_task_slug,
)


@app.command
def tasks(
    *,
    repo: str | None = None,
    status: str | None = None,
    project: str | None = None,
    assignee: str | None = None,
    ready: bool = False,
    all_repos: bool = False,
    limit: int = 50,
) -> None:
    """List tasks for the current repo (or filtered).

    Parameters
    ----------
    repo: Scope to a specific repo URI (default: the current git repo).
    status: Filter by open | in_progress | blocked | closed.
    project: Scope to a WorkflowProject (``wp-`` slug).
    assignee: Filter by owner.
    ready: Show only ready-to-work tasks (open, all blockers closed).
    all_repos: Span every repo in the graph.
    limit: Max rows.
    """
    s = _srv()
    repo_arg = _repo_arg(repo, all_repos)
    detected_repo = (
        _detect_repo_for_display() if not all_repos and repo is None else repo
    )

    if ready:
        rows = _fn(s.task_ready)(
            repo=repo_arg, project_slug=project, assignee=assignee, limit=limit
        )
    else:
        rows = _fn(s.task_list)(
            repo=repo_arg, status=status, project_slug=project, assignee=assignee
        )[:limit]

    if not rows:
        if detected_repo and not all_repos:
            console.print(
                f"[dim]No tasks scoped to {_short_repo(detected_repo)}.[/dim] "
                f"Tasks may have been created without repo context. "
                f"Try [bold]--all-repos[/bold] to see all tasks."
            )
        else:
            console.print("[dim]No tasks.[/dim]")
        return

    if all_repos:
        scope = "all repos"
    elif detected_repo:
        scope = _short_repo(detected_repo)
    else:
        scope = "all repos (no git context)"
    base_title = "Ready tasks" if ready else "Tasks"
    table = Table(title=f"{base_title} — {scope}", header_style="bold")
    _short_cols = {"priority", "status", "type"}
    for col in (
        "priority",
        "status",
        "type",
        "slug",
        "title",
        "repo",
        "assignee",
        "blocked_by",
    ):
        if col in _short_cols:
            table.add_column(col, no_wrap=True)
        else:
            table.add_column(col, overflow="fold", no_wrap=False)
    for r in rows:
        repo_display = _short_repo(r.get("repo")) or "[dim](unscoped)[/dim]"
        table.add_row(
            _styled(r.get("priority", ""), _PRIORITY_STYLE),
            _styled(r.get("status", ""), _STATUS_STYLE),
            r.get("type", ""),
            r["slug"],
            r.get("title", ""),
            repo_display,
            r.get("assignee") or "",
            ", ".join(r.get("blocked_by") or []),
        )
    console.print(table)


def _task_show(slug: str) -> None:
    """Show one task's details, its sub-tasks, and blocker status."""
    s = _srv()
    t = _fn(s.task_get)(slug)
    if not t:
        console.print(f"[red]No task {slug!r}.[/red]")
        return

    console.print(f"[bold]{t['slug']}[/bold]  {t.get('title', '')}")
    console.print(
        f"  type={t.get('type')}  "
        f"priority={_styled(t.get('priority', ''), _PRIORITY_STYLE)}  "
        f"status={_styled(t.get('status', ''), _STATUS_STYLE)}"
    )
    for label, key in (
        ("repo", "repo"),
        ("project", "project_slug"),
        ("parent", "parent_slug"),
        ("assignee", "assignee"),
        ("reference", "external_uri"),
    ):
        if t.get(key):
            console.print(f"  {label}: {t[key]}")
    if t.get("symbol_refs"):
        console.print(f"  code symbols: {', '.join(t['symbol_refs'])}")
    console.print(f"\n{t.get('description') or '(no description)'}\n")

    for blocker in t.get("blocked_by") or []:
        b = _fn(s.task_get)(blocker)
        st = b.get("status") if b else "missing"
        console.print(f"  blocked by {blocker} [{_styled(st, _STATUS_STYLE)}]")

    children = _fn(s.task_list)(parent=slug)
    for c in children:
        console.print(
            f"  ↳ {c['slug']} [{_styled(c.get('status', ''), _STATUS_STYLE)}] {c.get('title', '')}"
        )
    if t.get("resolution"):
        console.print(f"\n  resolution: {t['resolution']}")


task_app = cyclopts.App(
    name="task",
    help="Manage tasks.",
    default_command=_task_show,
)
app.command(task_app)


@task_app.command(name="create")
def task_create_cmd(
    title: str,
    *,
    description: str = "",
    type: TaskType = "task",
    priority: TaskPriority = "p2",
    repo: str | None = None,
    project: str | None = None,
    parent: str | None = None,
    blocked_by: list[str] | None = None,
    discovered_from: list[str] | None = None,
    external_uri: str | None = None,
    symbol_refs: list[str] | None = None,
    tags: list[str] | None = None,
) -> None:
    """Create a task in the work-coordination graph.

    Parameters
    ----------
    title: Short label for the work.
    description: Full description of the task.
    type: bug | feature | task | chore | epic.
    priority: p0 (highest) … p3.
    repo: Repo URI (default: auto-detected from git remote).
    project: wp- slug of the WorkflowProject this task rolls up to.
    parent: tk- slug of the parent task/epic.
    blocked_by: tk- slugs that must close before this task is ready.
    discovered_from: tk- slugs of tasks during which this work was discovered.
    external_uri: Reference URI (e.g. a GitHub issue or PR).
    symbol_refs: Code-graph symbol ids (repo#path::Name) this task concerns.
    tags: Optional free-form tags.
    """
    s = _srv()
    result = _fn(s.task_create)(
        title=title,
        description=description,
        type=type,
        priority=priority,
        repo=repo,
        project_slug=project,
        parent=parent,
        blocked_by=_split_csv(blocked_by),
        discovered_from=_split_csv(discovered_from),
        external_uri=external_uri,
        symbol_refs=_split_csv(symbol_refs),
        tags=_split_csv(tags),
    )
    console.print(f"[green]Created task:[/green] [bold]{result['slug']}[/bold]")
    console.print(f"  status: {_styled(result['status'], _STATUS_STYLE)}")
    if result.get("repo"):
        console.print(f"  repo: {_short_repo(result['repo'])}")


@task_app.command(name="run")
def task_run(
    slug: str | None = None,
    *,
    target: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    claim: bool = True,
    dry_run: bool = False,
    repo: str | None = None,
    all_repos: bool = False,
    project: str | None = None,
) -> None:
    """Claim one or more tasks and launch an agent to execute them.

    Without a slug, shows an interactive picker of ready tasks. Multiple
    selections offer a choice between a consolidated single-session prompt or
    running each task sequentially in separate agent invocations.

    Parameters
    ----------
    slug: Task slug to run directly (skips the picker).
    target: Named config target (overrides auto-detection).
    agent: Agent CLI to launch (claude, pi, copilot, opencode, kilo).
    model: Model flag passed to the agent.
    claim: Mark each task in_progress before launching.
    dry_run: Print the prompt(s) without launching or claiming.
    repo: Scope the picker to a specific repo URI.
    all_repos: Span all repos in the picker.
    project: Scope the picker to a specific wp- project slug.
    """
    try:
        cfg = cfg_module.load(target=target)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None

    if slug:
        _run_task_slug(
            slug, cfg=cfg, agent=agent, model=model, claim=claim, dry_run=dry_run
        )
        return

    s = _srv()
    repo_arg = _repo_arg(repo, all_repos)
    ready = _fn(s.task_ready)(repo=repo_arg, project_slug=project, limit=50)
    if not ready:
        console.print("[dim]No ready tasks.[/dim]")
        return

    console.print(f"[bold]Ready tasks[/bold] ({len(ready)} available):\n")

    def _render_task(t: dict) -> str:
        pri = _styled(t.get("priority", ""), _PRIORITY_STYLE)
        repo_s = f"  [dim]{_short_repo(t.get('repo'))}[/dim]" if t.get("repo") else ""
        return f"{t['slug']}  {pri}  {t.get('title', '')}{repo_s}"

    selected = _pick_items(ready, _render_task)
    if not selected:
        console.print("[dim]Nothing selected.[/dim]")
        return

    resolved_agent = agent or cfg.agent
    resolved_model = model or cfg.model

    if len(selected) == 1:
        _run_task_slug(
            selected[0]["slug"],
            cfg=cfg,
            agent=agent,
            model=model,
            claim=claim,
            dry_run=dry_run,
        )
        return

    console.print(f"\n[bold]{len(selected)} tasks selected.[/bold]")
    console.print("  [1] Consolidate: one agent session covering all tasks")
    console.print("  [2] Sequential: a separate agent invocation per task")
    try:
        choice = Prompt.ask("Choice", choices=["1", "2"], default="1", console=console)
    except (EOFError, KeyboardInterrupt):
        console.print("\n[yellow]Cancelled.[/yellow]")
        raise SystemExit(0)
    mode = "c" if choice == "1" else "s"

    if mode == "c":
        claimable = list(selected)
        if claim and not dry_run:
            claimable = []
            for t in selected:
                res = _fn(s.task_claim)(t["slug"], assignee=cfg.author) or {}
                if res.get("claimed"):
                    console.print(f"[cyan]Claimed {t['slug']}.[/cyan]")
                    claimable.append(t)
                else:
                    reason = res.get("held_by") or res.get("reason") or "unavailable"
                    console.print(
                        f"[yellow]Could not claim {t['slug']} ({reason}), skipping.[/yellow]"
                    )
        if not claimable:
            console.print("[red]No tasks could be claimed.[/red]")
            raise SystemExit(1)
        merged = _merge_prompts([_run_prompt(t) for t in claimable], "task")
        _launch_agent(cfg, resolved_agent, resolved_model, merged, dry_run)
    else:
        for t in selected:
            console.print(f"\n[bold]── {t['slug']}: {t.get('title', '')} ──[/bold]")
            _run_task_slug(
                t["slug"],
                cfg=cfg,
                agent=agent,
                model=model,
                claim=claim,
                dry_run=dry_run,
            )
