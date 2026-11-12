"""Memory commands: search/list, and import from local agent memory."""

from __future__ import annotations

from collections import Counter
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Annotated

import cyclopts

from witan_core.refusal import Refusal
from witan_core.repo_key import normalise

from .. import memory_import as local
from ._common import (
    MemoryKind,
    _fn,
    _repo_arg,
    _short_repo,
    _split_csv,
    _srv,
    app,
    esc,
    print_error,
    print_notice,
    render_table,
)


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


def _norm(text: object) -> str:
    return " ".join(str(text or "").split()).casefold()


def _existing(recall, mem: local.LocalMemory, repo: str) -> str | None:
    """Slug of a memory already in the graph that this file duplicates.

    Uses ``recall``, the search the witan-memory skill tells agents to run
    before storing. Superseded memories count, so an import cannot resurrect
    knowledge somebody already replaced. A hit counts as a duplicate when its
    title matches, or its full content does (a cut-short ``content`` cannot be
    compared).
    """
    hits = recall(query=mem.name, repo=repo, hops=0, include_superseded=True)
    for hit in hits["memories"]:
        if _norm(hit.get("title")) == _norm(mem.name):
            return hit["slug"]
        if not hit.get("content_truncated") and _norm(hit.get("content")) == _norm(
            mem.content
        ):
            return hit["slug"]
    return None


def _matches_repo(wanted: str, repo: str | None) -> bool:
    if repo is None:
        return False
    wanted = wanted.strip().rstrip("/")
    return wanted.casefold() == _short_repo(repo).casefold() or (
        normalise(wanted) == repo
    )


@memory_app.command(name="import")
def memory_import(
    *,
    source: Path | None = None,
    repo: str | None = None,
    select: list[str] | None = None,
    all_: Annotated[bool, cyclopts.Parameter(name="--all")] = False,
    confirm: bool = False,
) -> None:
    """Import Claude Code's local memory files into the shared graph.

    Reads ``<source>/<project>/memory/*.md`` and reports, for each file, the
    memory kind it maps to, the repo its project resolves to, and whether the
    graph already holds it. Nothing is written unless --confirm is passed with
    --select or --all. Each write goes through memory_store, so the secret and
    PII scan applies, and a blocked write is reported rather than stored.

    Claude Code types map to kinds as project and reference to project_fact,
    feedback to lesson, and user to agent_context. A user memory is about a
    person, so --all and glob patterns skip it; name it exactly in --select to
    include it.

    Parameters
    ----------
    source:
        Directory of Claude Code project folders. Defaults to
        $CLAUDE_CONFIG_DIR/projects, else ~/.claude/projects.
    repo:
        Only consider memories whose project resolves to this repo, given as a
        URI or as org/name.
    select:
        Memories to store, by the id shown in the dry run, by file stem, or by
        a glob over either. Repeatable or comma-separated.
    all_:
        Select every memory that is not already in the graph, except user ones.
    confirm:
        Write the selected memories. Without it the command only reports.
    """
    src = source or local.default_source()
    if not src.is_dir():
        print_error(f"No such directory: {src}")
        raise SystemExit(1)
    patterns = _split_csv(select) or []
    if confirm and not (patterns or all_):
        print_error("--confirm writes only what --select or --all chooses.")
        raise SystemExit(1)

    memories = local.discover(src)
    resolutions = {
        d: local.resolve_project(d)
        for d in dict.fromkeys(m.project_dir for m in memories)
    }
    if repo is not None:
        memories = [
            m for m in memories if _matches_repo(repo, resolutions[m.project_dir].repo)
        ]

    def ident(mem: local.LocalMemory) -> str:
        res = resolutions[mem.project_dir]
        label = _short_repo(res.repo) if res.repo else mem.project_dir.lstrip("-")
        return f"{label}/{mem.path.stem}"

    def named(mem: local.LocalMemory, pattern: str) -> bool:
        return pattern in (ident(mem), mem.path.stem)

    def globbed(mem: local.LocalMemory, pattern: str) -> bool:
        return fnmatchcase(ident(mem), pattern) or fnmatchcase(mem.path.stem, pattern)

    unmatched = [p for p in patterns if not any(globbed(m, p) for m in memories)]
    if unmatched:
        print_error(f"--select matched no memory: {', '.join(unmatched)}")
        raise SystemExit(1)

    from ..remote.proxy import RemoteToolFailed

    s = _srv()
    recall = _fn(s.recall)
    store = _fn(s.memory_store)
    claimed: set[tuple[str, str]] = set()
    rows: list[dict[str, object]] = []
    for mem in memories:
        res = resolutions[mem.project_dir]
        row: dict[str, object] = {
            "id": ident(mem),
            "kind": mem.kind,
            "repo": res.repo,
            "status": "",
            "detail": "",
            "title": mem.name,
            "path": str(mem.path),
        }
        rows.append(row)
        if mem.error:
            row.update(status="invalid", detail=mem.error)
            continue
        if res.repo is None:
            row.update(status="unresolved", detail="project has no repo")
            continue
        key = (res.repo, _norm(mem.name))
        if dup := _existing(recall, mem, res.repo):
            row.update(status="duplicate", detail=dup)
            continue
        if key in claimed:
            row.update(status="duplicate", detail="same title earlier in this run")
            continue
        is_user = mem.type == "user"
        chosen = any(named(mem, p) for p in patterns) or (
            not is_user and (all_ or any(globbed(mem, p) for p in patterns))
        )
        if not chosen:
            if is_user:
                row.update(status="user", detail="name it in --select to include")
            else:
                row.update(status="new")
            continue
        claimed.add(key)
        if not confirm:
            row.update(status="would store")
            continue
        try:
            result = store(
                kind=mem.kind,
                title=mem.name,
                content=mem.content,
                repo=res.repo,
                category="reference" if mem.type == "reference" else None,
            )
        except (Refusal, RemoteToolFailed) as exc:
            # A local store raises the scanner's WriteBlocked (a Refusal); a
            # deployed one returns the same refusal as RemoteToolFailed.
            claimed.discard(key)
            row.update(status="blocked", detail=str(exc))
            continue
        row.update(
            status="stored",
            detail=" ".join(
                part for part in (result["slug"], result.get("redaction_note")) if part
            ),
        )

    render_table(
        title=f"Local memories in {src}",
        columns=["id", "kind", "status", "detail", "title"],
        rows=rows,
        empty="[dim]No memory files found.[/dim]",
        no_wrap={"kind", "status"},
        styles={
            "status": {
                "new": "green",
                "would store": "cyan",
                "stored": "bold green",
                "duplicate": "dim",
                "user": "yellow",
                "unresolved": "yellow",
                "invalid": "red",
                "blocked": "red",
            }
        },
    )
    for project_dir, res in resolutions.items():
        if res.repo is None and any(m.project_dir == project_dir for m in memories):
            print_notice(
                f"[yellow]unresolved[/yellow] {esc(project_dir)}: {esc(res.reason)}"
            )
    counts = Counter(str(r["status"]) for r in rows)
    summary = ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
    print_notice(f"{len(rows)} memory files: {esc(summary or 'none')}.")
    if not confirm:
        print_notice(
            "[dim]Dry run, nothing written. Pass --confirm with --select or --all "
            "to store.[/dim]"
        )
    if counts["blocked"]:
        raise SystemExit(1)
