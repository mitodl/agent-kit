"""The ``witan`` umbrella CLI.

Exposed as ``witan`` (see pyproject ``[project.scripts]``). Covers the
work-coordination graph (tasks, workflow projects, memory), starts the MCP
server (``witan serve``), and — when ``witan-code`` is installed — mounts the
code-graph tool as ``witan code …``.

It is a thin presentation layer: every query goes through the same
``witan.server`` tool functions the MCP server exposes, so behaviour
(repo scoping, ready-work computation, …) stays identical.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Literal

import cyclopts
from rich.console import Console
from rich.table import Table

from . import config as cfg_module
from . import repo as repo_module

# Memory kinds, mirrored from witan.server.MemoryKind — drives the `--kind`
# enum/validation in `witan memory`.
MemoryKind = Literal["pattern", "project_fact", "lesson", "agent_context"]

TaskType = Literal["bug", "feature", "task", "chore", "epic"]
TaskPriority = Literal["p0", "p1", "p2", "p3"]
WorkflowPhase = Literal["discovery", "spec", "implementation", "delivery"]


def _split_csv(items: list[str] | None) -> list[str] | None:
    if items is None:
        return None
    return [x.strip() for item in items for x in item.split(",") if x.strip()] or None


app = cyclopts.App(
    name="witan",
    help="witan — agent memory, planning, and collaboration graph.",
)
console = Console()

# Mount the code-graph CLI as `witan code …` when witan-code is installed.
# Optional: the umbrella works standalone without it.
try:
    from witan_code.cli import app as _code_app

    app.command(_code_app, name="code")
except ImportError:
    pass


@app.command
def serve() -> None:
    """Run the witan MCP server.

    Serves the work-coordination tools (memory_*, task_*, workflow_*) and, when
    witan-code is installed, mounts the code-graph tools (code_*) into the same
    server so a single MCP entry exposes everything.
    """
    from .server import mcp as witan_mcp

    try:
        from witan_code.server import mcp as code_mcp

        witan_mcp.mount(code_mcp, prefix=None)
    except ImportError:
        pass
    witan_mcp.run()


# ── Lazy server access (importing server builds an OmnigraphClient, which needs
#    the omnigraph binary — defer it so `--help` works without one). ──────────

_server = None


def _srv():
    global _server
    if _server is None:
        from . import server as server_module

        _server = server_module
    return _server


def _fn(tool):
    """Unwrap a FastMCP-decorated tool to its plain function."""
    return getattr(tool, "fn", tool)


def _repo_arg(repo: str | None, all_repos: bool) -> str | None:
    """Map --repo/--all-repos to the server tools' ``repo`` parameter.

    ``""`` means all repos; ``None`` means detect the current repo; a string
    scopes to that repo.
    """
    return "" if all_repos else repo


_PRIORITY_STYLE = {"p0": "bold red", "p1": "red", "p2": "yellow", "p3": "dim"}
_STATUS_STYLE = {
    "open": "green",
    "in_progress": "cyan",
    "blocked": "red",
    "closed": "dim",
    "active": "green",
    "completed": "blue",
    "abandoned": "dim",
}


def _styled(value: str, table: dict) -> str:
    style = table.get(value)
    return f"[{style}]{value}[/{style}]" if style else (value or "")


def _short_repo(uri: str | None) -> str:
    """Strip scheme+host from a repo URI for compact display."""
    if not uri:
        return ""
    m = re.match(r"https?://[^/]+/(.+)", uri)
    return m.group(1) if m else uri


def _detect_repo_for_display() -> str | None:
    """Detect current repo URI for CLI display/filtering."""
    return repo_module.detect()


# ── Tasks ──────────────────────────────────────────────────────────────────


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
        table.add_column(col)
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


@app.command
def run(
    slug: str,
    *,
    target: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    claim: bool = True,
    dry_run: bool = False,
) -> None:
    """Claim a task and launch an agent to execute it.

    Claims the task (status in_progress, assignee = your author), then hands the
    terminal to ``<agent>`` seeded with a prompt describing the work. Run from
    the task's repo checkout so the agent has the right working directory.

    Parameters
    ----------
    target: Named config target to use (overrides auto-detection by repo org).
        Also overridable via WITAN_TARGET env var.
    agent: Agent CLI to launch (claude, pi, copilot, opencode, kilo). Overrides
        WITAN_AGENT env var and target/config-file default.
    model: Model passed to the agent's --model flag. Overrides WITAN_MODEL env
        var and target/config-file default.
    claim: Mark the task in_progress and assign it to you first.
    dry_run: Print the prompt and exit without launching or claiming.
    """
    try:
        cfg = cfg_module.load(target=target)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None

    resolved_agent = agent or cfg.agent
    resolved_model = model or cfg.model

    s = _srv()
    t = _fn(s.task_get)(slug)
    if not t:
        console.print(f"[red]No task {slug!r}.[/red]")
        raise SystemExit(1)

    open_blockers = [
        b
        for b in (t.get("blocked_by") or [])
        if (_fn(s.task_get)(b) or {}).get("status") != "closed"
    ]
    if open_blockers:
        console.print(
            f"[yellow]Warning: open blockers: {', '.join(open_blockers)}[/yellow]"
        )

    prompt = _run_prompt(t)
    if dry_run:
        console.print(prompt)
        return

    if claim:
        res = _fn(s.task_claim)(slug, assignee=cfg.author) or {}
        if not res.get("claimed"):
            reason = res.get("held_by") or res.get("reason") or "unavailable"
            console.print(f"[red]Could not claim {slug} ({reason}).[/red]")
            raise SystemExit(1)
        console.print(f"[cyan]Claimed {slug} (assignee={cfg.author}).[/cyan]")

    cmd = [resolved_agent]
    if resolved_model:
        cmd += ["--model", resolved_model]
    cmd.append(prompt)

    target_info = f" [{cfg.target_name}]" if cfg.target_name else ""
    model_info = f" --model {resolved_model}" if resolved_model else ""
    console.print(f"[dim]Launching: {resolved_agent}{model_info}{target_info}[/dim]")
    try:
        subprocess.run(cmd, check=False)
    except FileNotFoundError:
        console.print(
            f"[red]Agent {resolved_agent!r} not found on PATH.[/red] "
            f"Task is claimed; run your agent manually with --dry-run's prompt."
        )
        raise SystemExit(1) from None


def _run_prompt(t: dict) -> str:
    lines = [
        "Work on this task from the omnigraph task graph and see it through to completion.",
        "",
        f"Task:     {t['slug']}",
        f"Title:    {t.get('title', '')}",
        f"Type:     {t.get('type')}    Priority: {t.get('priority')}    Status: {t.get('status')}",
    ]
    for label, key in (
        ("Repo", "repo"),
        ("Project", "project_slug"),
        ("Reference", "external_uri"),
    ):
        if t.get(key):
            lines.append(f"{label}:{' ' * (10 - len(label))}{t[key]}")
    if t.get("symbol_refs"):
        lines.append(
            f"Symbols:  {', '.join(t['symbol_refs'])}  (use the code_* tools to inspect)"
        )
    lines += [
        "",
        "Description:",
        t.get("description") or "(none)",
        "",
        f'When complete, close it: task_close(slug="{t["slug"]}", resolution="<what you did>"). '
        f'File any follow-up work with task_create(discovered_from=["{t["slug"]}"], ...).',
    ]
    return "\n".join(lines)


# ── Workflow projects ────────────────────────────────────────────────────────


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
    for col in ("status", "phase", "slug", "title", "repos"):
        table.add_column(col)
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


project_app = cyclopts.App(
    name="project",
    help="Manage workflow projects.",
    default_command=_project_show,
)
app.command(project_app)


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


# ── Memory ───────────────────────────────────────────────────────────────────


@app.command
def memory(
    query: str | None = None,
    *,
    kind: MemoryKind | None = None,
    repo: str | None = None,
    all_repos: bool = False,
    limit: int = 20,
) -> None:
    """Search memory (BM25), or with no query list memories (filtered by --kind)."""
    s = _srv()
    repo_arg = _repo_arg(repo, all_repos)
    if query:
        rows = _fn(s.memory_search)(query, repo=repo_arg, kind=kind)[:limit]
        title = f"Memory search: {query!r}"
    else:
        rows = _fn(s.memory_list)(kind=kind, repo=repo_arg)[:limit]
        title = f"Memories ({kind})" if kind else "Memories"
    if not rows:
        console.print("[dim]No memories.[/dim]")
        return
    table = Table(title=title, header_style="bold")
    for col in ("kind", "slug", "title", "repo"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            r.get("kind", "project_fact"),
            r["slug"],
            r.get("title", ""),
            r.get("repo", "") or "",
        )
    console.print(table)


@app.command(name="inject-context")
def inject_context() -> None:
    """Print workflow context for the UserPromptSubmit hook.

    Emits active WorkflowProjects and ready Tasks for the current git repo to
    stdout. Designed to be called by ``~/.claude/hooks/workflow-context-inject.sh``
    — always exits 0 and never blocks even when the graph is missing or the repo
    is not in git.
    """
    from . import context as ctx_module

    cfg = cfg_module.load()
    graph_path = (
        Path(cfg.graph_uri)
        if not cfg.graph_uri.startswith(("http://", "https://", "s3://"))
        else None
    )
    if graph_path is not None and not graph_path.exists():
        return
    text = ctx_module.inject_context(cfg.graph_uri, cfg.queries_dir, cfg.graph_token)
    if text:
        print(text)


@app.command(name="session-checkpoint")
def session_checkpoint() -> None:
    """Auto-close the active WorkflowSession on agent stop (Stop hook).

    Reads the state file written by ``workflow_session_start`` and records an
    end timestamp via ``update_workflow_session_end``. No-op when the file is
    absent — always exits 0 and never blocks.
    """
    from . import context as ctx_module

    cfg = cfg_module.load()
    ctx_module.session_checkpoint(cfg.graph_uri, cfg.queries_dir, cfg.graph_token)


_AGENTS = ("claude", "pi", "copilot", "opencode", "kilo")
_AGENT_NAMES = {
    "claude": "Claude Code",
    "pi": "Pi",
    "copilot": "GitHub Copilot",
    "opencode": "OpenCode",
    "kilo": "Kilo Code",
}
AgentName = Literal["claude", "pi", "copilot", "opencode", "kilo", "all"]


@app.command
def setup(
    *,
    agent: AgentName = "claude",
    author: str | None = None,
    dry_run: bool = False,
) -> None:
    """Install witan for one or all supported coding agents.

    Installs the omnigraph binary to ``~/.local/bin/``, copies bundled skills
    and hooks/extensions to the agent's config directories, and merges the
    witan MCP server entry into the agent's config file.

    Re-run after every upgrade to refresh installed files.

    Parameters
    ----------
    agent: Target agent — claude | pi | copilot | opencode | kilo | all.
    author: Name written to graph nodes (default: git config user.name or $USER).
    dry_run: Print what would happen without writing anything.
    """
    from . import setup as su

    pkg_dir = Path(__file__).parent

    if author is None:
        try:
            author = subprocess.check_output(
                ["git", "config", "user.name"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            author = ""
        author = author or os.environ.get("USER", "unknown")

    if not shutil.which("witan") and not dry_run:
        console.print(
            "[yellow]Warning:[/yellow] witan not on PATH. "
            "Hooks calling [bold]witan inject-context[/bold] will fail until:\n"
            "  [bold]uv tool install "
            "git+https://github.com/mitodl/agent-kit"
            "#subdirectory=mcp/servers/witan[/bold]"
        )

    _installers = {
        "claude": su.install_claude,
        "pi": su.install_pi,
        "copilot": su.install_copilot,
        "opencode": su.install_opencode,
        "kilo": su.install_kilo,
    }
    _detectors = {
        "claude": lambda: True,
        "pi": su.is_pi_installed,
        "copilot": su.is_copilot_installed,
        "opencode": su.is_opencode_installed,
        "kilo": su.is_kilo_installed,
    }

    targets = list(_AGENTS) if agent == "all" else [agent]

    console.print("[bold]omnigraph binary[/bold]")
    su.install_omnigraph(pkg_dir, dry_run)

    for ag in targets:
        if agent == "all" and not _detectors[ag]():
            console.print(f"\n[dim]{_AGENT_NAMES[ag]} — not detected, skipping[/dim]")
            continue
        console.print(f"\n[bold]{_AGENT_NAMES[ag]}[/bold]")
        _installers[ag](pkg_dir, author, dry_run)

    if dry_run:
        console.print("\n[dim](dry-run — no files written)[/dim]")
    else:
        console.print(
            "\n[bold green]Done.[/bold green] "
            "Restart your agent(s) to pick up the new MCP server and hooks."
        )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
