"""Graph visualization command: ``witan graph``."""

from __future__ import annotations

from pathlib import Path

from ._common import _fn, _repo_arg, _srv, app, console


@app.command
def graph(
    *,
    repo: str | None = None,
    all_repos: bool = False,
    status: str | None = "active",
    all_tasks: bool = False,
    no_belongs_to: bool = False,
    html: Path | None = None,
    dot: Path | None = None,
    open_browser: bool = False,
) -> None:
    """Visualize the workflow project and task dependency graph.

    Prints a Rich summary of projects and tasks, then optionally writes an
    interactive HTML graph (vis-network) or a Graphviz DOT file.

    Parameters
    ----------
    repo:
        Scope to a specific repo URI (default: current git repo).
    all_repos:
        Include projects and tasks from every repo.
    status:
        Project status filter: active | completed | abandoned.
        Defaults to ``active``. Pass an empty string to include all.
    all_tasks:
        Include closed tasks (default: open + in_progress + blocked only).
    no_belongs_to:
        Omit dashed task→project edges to reduce clutter.
    html:
        Write a self-contained interactive HTML graph to this path.
    dot:
        Write a Graphviz DOT file to this path.
    open_browser:
        Open the generated HTML in the default browser (requires --html).
    """
    from .. import visualize

    s = _srv()
    repo_arg = _repo_arg(repo, all_repos)

    projects = _fn(s.workflow_project_list)(repo=repo_arg, status=status or None)
    project_slugs = {p["slug"] for p in projects}

    tasks_raw = _fn(s.task_list)(repo=repo_arg, status=None)
    if not all_tasks:
        tasks_raw = [t for t in tasks_raw if t.get("status") != "closed"]

    # Keep tasks belonging to fetched projects or tasks without a project (when
    # filtering by repo). When all_repos is active, include all tasks.
    if project_slugs:
        tasks = [
            t
            for t in tasks_raw
            if not t.get("project_slug") or t.get("project_slug") in project_slugs
        ]
    else:
        tasks = tasks_raw

    g = visualize.build_graph(projects, tasks, show_belongs_to=not no_belongs_to)
    visualize.render_rich(g, console)

    if html is not None:
        out = visualize.render_html(g, html)
        console.print(f"\nwrote {out}")
        if open_browser:
            import webbrowser

            webbrowser.open(out.resolve().as_uri())

    if dot is not None:
        out = visualize.render_dot(g, dot)
        console.print(f"wrote {out}")
