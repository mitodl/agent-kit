"""Memory search and list command, and the stale symbol-ref review."""

from __future__ import annotations

import cyclopts

from ._common import MemoryKind, _fn, _repo_arg, _srv, app, render_table


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
        empty="[dim]No memories.[/dim]",
        no_wrap={"kind"},
        records=rows,
    )


memory_app = cyclopts.App(
    name="memory",
    help="Search memory (BM25), or with no query list memories (filtered by --kind).",
    default_command=memory,
)
app.command(memory_app)


@memory_app.command(name="stale-refs")
def stale_refs(*, repo: str | None = None, all_repos: bool = False) -> None:
    """List memories whose symbol_refs no longer resolve in the code graph.

    A review list, never an edit. ``missing`` means the repo is indexed and the
    symbol is not, which may be a rename: the detail column lists indexed
    symbols with the same name. ``unknown`` means the ref could not be checked
    (the repo is not indexed, or witan-code is unreachable).
    """
    result = _fn(_srv().memory_stale_refs)(repo=_repo_arg(repo, all_repos))
    rows = [
        {
            "status": ref["status"],
            "slug": m["slug"],
            "title": m.get("title") or "",
            "symbol_ref": ref["symbol_ref"],
            "detail": ref.get("reason") or ", ".join(ref.get("candidates") or []),
        }
        for m in result["memories"]
        for ref in m["refs"]
        if ref["status"] != "live"
    ]
    counts = result["counts"]
    render_table(
        title=(
            f"Stale symbol refs: {result['checked']} memories checked, "
            f"{counts['live']} live, {counts['missing']} missing, "
            f"{counts['unknown']} unknown"
        ),
        columns=["status", "slug", "title", "symbol_ref", "detail"],
        rows=rows,
        empty="[dim]No stale symbol refs.[/dim]",
        no_wrap={"status"},
        styles={"status": {"missing": "red", "unknown": "yellow"}},
        records=result["memories"],
    )
