"""cyclopts CLI for exploring the omnigraph graphs.

Exposed as ``omnigraph-explore`` (see pyproject ``[project.scripts]``). A
read-mostly inspector over the work-coordination graph (tasks, workflow
projects, memory) and the per-repo code graphs, plus ``run`` to claim a task and
hand it to an agent.

It is a thin presentation layer: every query goes through the same
``omnigraph_memory.server`` tool functions the MCP server exposes, so behaviour
(repo scoping, ready-work computation, …) stays identical.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import cyclopts
from rich.console import Console
from rich.table import Table

from . import config as cfg_module

app = cyclopts.App(
    name="omnigraph-explore",
    help="Explore the omnigraph work-coordination and code graphs.",
)
console = Console()

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
    if ready:
        rows = _fn(s.task_ready)(
            repo=repo_arg, project_slug=project, assignee=assignee, limit=limit
        )
    else:
        rows = _fn(s.task_list)(
            repo=repo_arg, status=status, project_slug=project, assignee=assignee
        )[:limit]

    if not rows:
        console.print("[dim]No tasks.[/dim]")
        return

    table = Table(title="Ready tasks" if ready else "Tasks", header_style="bold")
    for col in (
        "priority",
        "status",
        "type",
        "slug",
        "title",
        "assignee",
        "blocked_by",
    ):
        table.add_column(col)
    for r in rows:
        table.add_row(
            _styled(r.get("priority", ""), _PRIORITY_STYLE),
            _styled(r.get("status", ""), _STATUS_STYLE),
            r.get("type", ""),
            r["slug"],
            r.get("title", ""),
            r.get("assignee") or "",
            ", ".join(r.get("blocked_by") or []),
        )
    console.print(table)


@app.command
def task(slug: str) -> None:
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


@app.command
def run(
    slug: str, *, agent: str = "claude", claim: bool = True, dry_run: bool = False
) -> None:
    """Claim a task and launch an agent to execute it.

    Claims the task (status in_progress, assignee = your author), then hands the
    terminal to ``<agent>`` seeded with a prompt describing the work. Run from
    the task's repo checkout so the agent has the right working directory.

    Parameters
    ----------
    agent: Agent CLI to launch (e.g. ``claude`` or ``pi``).
    claim: Mark the task in_progress and assign it to you first.
    dry_run: Print the prompt and exit without launching or claiming.
    """
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
        author = cfg_module.load().author
        _fn(s.task_update)(slug, status="in_progress", assignee=author)
        console.print(f"[cyan]Claimed {slug} (assignee={author}).[/cyan]")

    console.print(f"[dim]Launching: {agent}[/dim]")
    try:
        subprocess.run([agent, prompt], check=False)
    except FileNotFoundError:
        console.print(
            f"[red]Agent {agent!r} not found on PATH.[/red] "
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
    table = Table(title="Workflow projects", header_style="bold")
    for col in ("status", "phase", "slug", "title", "repo"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            _styled(r.get("status", ""), _STATUS_STYLE),
            r.get("phase", ""),
            r["slug"],
            r.get("title", ""),
            r.get("repo", "") or "",
        )
    console.print(table)


@app.command
def project(slug: str) -> None:
    """Show a project, its sessions, and rolled-up tasks."""
    s = _srv()
    p = _fn(s.workflow_project_get)(slug)
    if not p:
        console.print(f"[red]No project {slug!r}.[/red]")
        return
    console.print(f"[bold]{p['slug']}[/bold]  {p.get('title', '')}")
    console.print(
        f"  status={_styled(p.get('status', ''), _STATUS_STYLE)}  "
        f"phase={p.get('phase')}  repo={p.get('repo')}"
    )
    if p.get("github_issue"):
        console.print(f"  issue: {p['github_issue']}")
    if p.get("github_pr"):
        console.print(f"  pr: {p['github_pr']}")
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


# ── Memory ───────────────────────────────────────────────────────────────────


@app.command
def memory(
    query: str | None = None,
    *,
    kind: str | None = None,
    repo: str | None = None,
    all_repos: bool = False,
    limit: int = 20,
) -> None:
    """Search memory (BM25) or, with no query, list the repo's project facts."""
    s = _srv()
    repo_arg = _repo_arg(repo, all_repos)
    if query:
        rows = _fn(s.memory_search)(query, repo=repo_arg, kind=kind)[:limit]
        title = f"Memory search: {query!r}"
    else:
        rows = _fn(s.memory_get_project_facts)(repo=repo_arg)[:limit]
        title = "Project facts"
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


# ── Indexed code repos ───────────────────────────────────────────────────────

_CODE_DIR = Path(
    os.environ.get(
        "OMNIGRAPH_CODEGRAPH_DIR",
        str(Path.home() / ".local" / "share" / "omnigraph-memory" / "code"),
    )
)
_CODEGRAPH_READ = (
    Path(__file__).resolve().parents[2] / "omnigraph-codegraph" / "queries" / "read.gq"
)


@app.command
def repos() -> None:
    """List the repositories that have a code graph indexed."""
    if not _CODE_DIR.is_dir():
        console.print(f"[dim]No code stores at {_CODE_DIR}.[/dim]")
        return
    stores = sorted(_CODE_DIR.glob("*.omni"))
    if not stores:
        console.print(f"[dim]No code stores at {_CODE_DIR}.[/dim]")
        return

    table = Table(title="Indexed repositories", header_style="bold")
    for col in ("repo", "files", "size", "last indexed"):
        table.add_column(col)
    for store in stores:
        repo_uri, file_count = _code_store_stats(store)
        table.add_row(
            repo_uri,
            str(file_count),
            _human_size(_dir_size(store)),
            _mtime(store),
        )
    console.print(table)


def _code_store_stats(store: Path) -> tuple[str, str]:
    """Return (repo_uri, file_count) by reading the store; fall back to the name."""
    if _CODEGRAPH_READ.exists():
        try:
            from .graph import OmnigraphClient

            client = OmnigraphClient(str(store), _CODEGRAPH_READ.parent)
            rows = client.read("read.gq", "all_file_hashes", {})
            if rows:
                repo_uri = rows[0]["slug"].split("#", 1)[0]
                return repo_uri, str(len(rows))
            return store.stem, "0"
        except Exception:  # noqa: BLE001 — degrade to the filename
            pass
    return store.stem, "?"


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def _mtime(path: Path) -> str:
    import datetime

    ts = max(
        (f.stat().st_mtime for f in path.rglob("*") if f.is_file()),
        default=path.stat().st_mtime,
    )
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
