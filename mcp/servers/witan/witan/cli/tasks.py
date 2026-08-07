"""Task commands: list, show, create, run."""

from __future__ import annotations

import cyclopts
from rich.prompt import Prompt

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
    TaskLinkKind,
    TaskPriority,
    TaskStatus,
    TaskType,
    app,
    console,
    render_table,
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

    Closed tasks are elided by default — the list is a working view of live work.
    Pass ``--status closed`` to see them (or any other status to filter to it).

    Parameters
    ----------
    repo: Scope to a specific repo URI (default: the current git repo).
    status: Filter by open | in_progress | blocked | closed. Omitted: all
        non-closed statuses.
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
        )
        # A bare `witan tasks` is a live-work view: drop closed tasks unless the
        # user explicitly asks for a status (including `--status closed`).
        if status is None:
            rows = [r for r in rows if r.get("status") != "closed"]
        rows = rows[:limit]

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
    rows_data = [
        {
            "priority": r.get("priority", ""),
            "status": r.get("status", ""),
            "type": r.get("type", ""),
            "slug": r["slug"],
            "title": r.get("title", ""),
            "repo": _short_repo(r.get("repo")) or "",
            "assignee": r.get("assignee") or "",
            "blocked_by": ", ".join(r.get("blocked_by") or []),
        }
        for r in rows
    ]
    render_table(
        title=f"{base_title} — {scope}",
        columns=[
            "priority",
            "status",
            "type",
            "slug",
            "title",
            "repo",
            "assignee",
            "blocked_by",
        ],
        rows=rows_data,
        no_wrap={"priority", "status", "type"},
        styles={"priority": _PRIORITY_STYLE, "status": _STATUS_STYLE},
        placeholders={"repo": "(unscoped)"},
    )


def _task_show(slug: str) -> None:
    """Show one task's details, its sub-tasks, and blocker status."""
    s = _srv()
    t = _fn(s.task_get)(slug=slug)
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
        ("parent", "parent_slug"),
        ("assignee", "assignee"),
        ("reference", "external_uri"),
    ):
        if t.get(key):
            console.print(f"  {label}: {t[key]}")
    if t.get("project_slug"):
        project = _fn(s.workflow_project_get)(slug=t["project_slug"])
        if project:
            console.print(
                f"  project: {project['slug']} — {project.get('title', '')} "
                f"[{project.get('phase', '')}]"
            )
        else:
            console.print(f"  project: {t['project_slug']}")
    if t.get("symbol_refs"):
        console.print(f"  code symbols: {', '.join(t['symbol_refs'])}")
    console.print(f"\n{t.get('description') or '(no description)'}\n")

    for blocker in t.get("blocked_by") or []:
        b = _fn(s.task_get)(slug=blocker)
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


@task_app.command(name="close")
def task_close_cmd(slug: str, *, resolution: str | None = None) -> None:
    """Close a task, recording an optional resolution.

    Closing a blocker unblocks its dependents.

    Parameters
    ----------
    slug: The ``tk-`` slug to close.
    resolution: Short note on what was done.
    """
    s = _srv()
    result = _fn(s.task_close)(slug=slug, resolution=resolution)
    if not result:
        console.print(f"[red]No task {slug!r}.[/red]")
        raise SystemExit(1)
    console.print(f"[green]Closed[/green] [bold]{slug}[/bold]")
    if resolution:
        console.print(f"  resolution: {resolution}")


@task_app.command(name="claim")
def task_claim_cmd(
    slug: str,
    *,
    assignee: str | None = None,
    force: bool = False,
) -> None:
    """Claim a task for work (status in_progress, with a lease).

    A live claim held by someone else is refused unless ``--force`` is passed
    (CLI has no interactive steal prompt).

    Parameters
    ----------
    slug: The ``tk-`` slug to claim.
    assignee: Holder identity (default: the author, qualified by this agent
        session so parallel sessions don't share one claim).
    force: Steal the task even if another holder's lease is still valid.
    """
    s = _srv()
    result = _fn(s.task_claim)(slug=slug, assignee=assignee, force=force)
    if result is None:
        console.print(f"[red]No task {slug!r}.[/red]")
        raise SystemExit(1)
    if result.get("claimed"):
        console.print(
            f"[green]Claimed[/green] [bold]{slug}[/bold] "
            f"(assignee={result.get('assignee')})"
        )
        return
    reason = result.get("held_by") or result.get("reason") or "unavailable"
    console.print(f"[yellow]Not claimed[/yellow] ({reason}).")
    if result.get("remedy"):
        console.print(f"  {result['remedy']}")
    raise SystemExit(1)


@task_app.command(name="release")
def task_release_cmd(
    slug: str,
    *,
    assignee: str | None = None,
    status: TaskStatus = "open",
    force: bool = False,
) -> None:
    """Release a claim, returning the task to ``open`` (or another status).

    Parameters
    ----------
    slug: The ``tk-`` slug to release.
    assignee: Holder identity releasing the task (default: the author, qualified
        by this agent session — a claim taken by another of your own sessions
        still matches, since the check is on identity, not session).
    status: Status to return the task to (default ``open``).
    force: Release even if held by a different assignee.
    """
    s = _srv()
    result = _fn(s.task_release)(
        slug=slug, assignee=assignee, status=status, force=force
    )
    if result is None:
        console.print(f"[red]No task {slug!r}.[/red]")
        raise SystemExit(1)
    if result.get("released"):
        console.print(
            f"[green]Released[/green] [bold]{slug}[/bold] → "
            f"{_styled(result.get('status', status), _STATUS_STYLE)}"
        )
        return
    console.print(
        f"[yellow]Not released[/yellow] — held by {result.get('held_by')} "
        f"(pass --force)."
    )
    if result.get("remedy"):
        console.print(f"  {result['remedy']}")
    raise SystemExit(1)


@task_app.command(name="update")
def task_update_cmd(
    slug: str,
    *,
    title: str | None = None,
    description: str | None = None,
    type: TaskType | None = None,
    priority: TaskPriority | None = None,
    status: TaskStatus | None = None,
    repo: str | None = None,
    project: str | None = None,
    parent: str | None = None,
    assignee: str | None = None,
    external_uri: str | None = None,
    tags: list[str] | None = None,
) -> None:
    """Update a task's mutable fields (only provided fields change).

    To *close* a task prefer ``task close``; to *claim* it prefer ``task claim``;
    to add dependencies use ``task link``.

    Parameters
    ----------
    slug: The ``tk-`` slug to update.
    title: New short label.
    description: New full description.
    type: bug | feature | task | chore | epic.
    priority: p0 (highest) … p3.
    status: open | in_progress | blocked | closed.
    repo: Canonical repo URI to (re)assign this task to.
    project: ``wp-`` slug of the WorkflowProject this task rolls up to.
    parent: ``tk-`` slug of the parent task/epic.
    assignee: Owner identity.
    external_uri: Reference URI (e.g. a GitHub issue or PR).
    tags: Replacement free-form tags.
    """
    s = _srv()
    result = _fn(s.task_update)(
        slug=slug,
        title=title,
        description=description,
        type=type,
        priority=priority,
        status=status,
        repo=repo,
        project_slug=project,
        parent=parent,
        assignee=assignee,
        external_uri=external_uri,
        tags=_split_csv(tags),
    )
    if not result:
        console.print(f"[red]No task {slug!r}.[/red]")
        raise SystemExit(1)
    console.print(f"[green]Updated[/green] [bold]{slug}[/bold]")
    console.print(
        f"  status={_styled(result.get('status', ''), _STATUS_STYLE)}  "
        f"priority={_styled(result.get('priority', ''), _PRIORITY_STYLE)}"
    )


@task_app.command(name="link")
def task_link_cmd(from_slug: str, to_slug: str, kind: TaskLinkKind) -> None:
    """Link two tasks (or a task to a memory).

    ``from``/``to`` meaning depends on ``kind``:
    blocks — from blocks to; parent — from is parent of to;
    discovered_from — from was discovered from to; addresses — from addresses
    memory to.

    Parameters
    ----------
    from_slug: Source ``tk-`` slug.
    to_slug: Target ``tk-`` (or memory) slug.
    kind: blocks | parent | discovered_from | addresses.
    """
    s = _srv()
    _fn(s.task_link)(from_slug=from_slug, to_slug=to_slug, kind=kind)
    console.print(f"[green]Linked[/green] {from_slug} —[{kind}]→ {to_slug}")


@task_app.command(name="unlink")
def task_unlink_cmd(from_slug: str, to_slug: str, kind: TaskLinkKind) -> None:
    """Remove a link between two tasks (or a task and a memory).

    The inverse of ``link``, with the same ``from``/``to`` meanings. Use it
    when a link was recorded backwards or against the wrong slug; removing a
    ``blocks`` link is how a wrongly-blocked task becomes ready again.

    Reports plainly when there was no such link — that is a no-op, not an
    error, so re-running is safe.

    Parameters
    ----------
    from_slug: Source ``tk-`` slug.
    to_slug: Target ``tk-`` (or memory) slug.
    kind: blocks | parent | discovered_from | addresses.
    """
    s = _srv()
    result = _fn(s.task_unlink)(from_slug=from_slug, to_slug=to_slug, kind=kind)
    if result.get("removed"):
        console.print(f"[green]Unlinked[/green] {from_slug} —[{kind}]→ {to_slug}")
    else:
        console.print(
            f"[yellow]No {kind} link[/yellow] {from_slug} → {to_slug}; nothing to do."
        )


@task_app.command(name="run")
def task_run(
    slug: str | None = None,
    *,
    target: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    claim: bool = True,
    force: bool = False,
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
    force: Take the claim even if someone else's lease is still live. Without
        this the command could report a task as held and offer no way past it
        from the CLI it was reported in — the interactive steal prompt is
        server-side and unreachable through ``_fn``, which passes no ``ctx``.
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
            slug,
            cfg=cfg,
            agent=agent,
            model=model,
            claim=claim,
            force=force,
            dry_run=dry_run,
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
            force=force,
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
                # No explicit assignee: let task_claim qualify the author with
                # this session, so a task another of these sessions is already
                # on reads as held instead of being silently re-claimed here.
                res = _fn(s.task_claim)(slug=t["slug"], force=force) or {}
                if res.get("claimed"):
                    console.print(f"[cyan]Claimed {t['slug']}.[/cyan]")
                    claimable.append(t)
                else:
                    reason = res.get("held_by") or res.get("reason") or "unavailable"
                    console.print(
                        f"[yellow]Could not claim {t['slug']} ({reason}), skipping.[/yellow]"
                    )
                    if res.get("remedy"):
                        console.print(f"  [yellow]{res['remedy']}[/yellow]")
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
                force=force,
                dry_run=dry_run,
            )
