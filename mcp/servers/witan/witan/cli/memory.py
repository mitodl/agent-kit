"""Memory search and list command."""

from __future__ import annotations

from ._common import MemoryKind, _fn, _repo_arg, _srv, app, console, render_table


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
        rows = _fn(s.memory_search)(query=query, repo=repo_arg, kind=kind)[:limit]
        title = f"Memory search: {query!r}"
    else:
        rows = _fn(s.memory_list)(kind=kind, repo=repo_arg)[:limit]
        title = f"Memories ({kind})" if kind else "Memories"
    if not rows:
        console.print("[dim]No memories.[/dim]")
        return
    rows_data = [
        {
            "kind": r.get("kind", "project_fact"),
            "slug": r["slug"],
            "title": r.get("title", ""),
            "repo": r.get("repo", "") or "",
        }
        for r in rows
    ]
    render_table(
        title=title,
        columns=["kind", "slug", "title", "repo"],
        rows=rows_data,
        no_wrap={"kind"},
    )
