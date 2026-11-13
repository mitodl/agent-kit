"""Memory commands: search/list, import from local agent memory, and the stale symbol-ref review."""

from __future__ import annotations

import re
from collections import Counter
from fnmatch import fnmatchcase
from typing import Annotated

import cyclopts

from agent_config_kit import (
    MemoryEntry,
    detect_installed_platforms,
    known_platforms,
)
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
        rows = _fn(s.memory_search)(query=query, kind=kind, **repo_arg)[:limit]
        title = f"Memory search: {query!r}"
    else:
        rows = _fn(s.memory_list)(kind=kind, **repo_arg)[:limit]
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


#: ``recall``'s query seed is two BM25 runs (content, then title) of 20 rows
#: each. Asking for both in full keeps an exact title match that ranks low in
#: the title band from being cut by 20 stronger content hits.
_DEDUPE_LIMIT = 40

#: What the scanner leaves in place of a redacted span (``scan.redact``).
_REDACTED = re.compile(r"«redacted:[^»]*»")


def _norm(text: object) -> str:
    return " ".join(str(text or "").split()).casefold()


def _same(stored: object, local_text: str) -> bool:
    """Whether stored text is ``local_text``, letting a redacted span stand for
    anything.

    The scanner redacts PII in a title or content on the way in, so a memory
    this command stored would otherwise never equal its source file again, and
    each re-run would store one more copy of it.
    """
    parts = _REDACTED.split(_norm(stored))
    if len(parts) == 1:
        return parts[0] == _norm(local_text)
    if not "".join(parts).strip():
        # Nothing but redaction left: it would match any text at all.
        return False
    pattern = ".+?".join(re.escape(part) for part in parts)
    return re.fullmatch(pattern, _norm(local_text), re.DOTALL) is not None


def _existing(recall, mem: MemoryEntry, repo: str) -> str | None:
    """Slug of a memory already in the graph that this file duplicates.

    Uses ``recall``, the search the witan-memory skill tells agents to run
    before storing. Superseded memories count, so an import cannot resurrect
    knowledge somebody already replaced. A hit counts as a duplicate when its
    title matches, or its full content does (a cut-short ``content`` cannot be
    compared).

    The query carries the description as well as the title. A title the
    scanner redacted whole (an address and nothing else) keeps none of its
    terms in the graph, so a title-only query could not find the stored copy
    and every rerun would store another.
    """
    hits = recall(
        query=" ".join(part for part in (mem.name, mem.description) if part),
        repo=repo,
        hops=0,
        limit=_DEDUPE_LIMIT,
        include_superseded=True,
    )
    for hit in hits["memories"]:
        if _same(hit.get("title"), mem.name):
            return hit["slug"]
        if not hit.get("content_truncated") and _same(hit.get("content"), mem.content):
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
    harness: list[str] | None = None,
    repo: str | None = None,
    select: list[str] | None = None,
    all_: Annotated[bool, cyclopts.Parameter(name="--all")] = False,
    confirm: bool = False,
) -> None:
    """Import the memory coding agents keep on disk into the shared graph.

    Reads the memory store of every agent detected on this machine (Claude
    Code's auto memory, Codex's memories, Copilot's repository memory; the
    agent-kit platform registry says where each lives) and reports, for each
    memory, the kind it maps to, the repo its project resolves to, and whether
    the graph already holds it. An agent with no memory store, or an empty
    one, is named in the output. Nothing is written unless --confirm is passed
    with --select or --all. Each write goes through memory_store, so the
    secret and PII scan applies, and a blocked write is reported rather than
    stored.

    Memories map to kinds by what they are about: project facts and
    references to project_fact, feedback and lessons to lesson, and user
    memories to agent_context. A user memory is about a person, so --all and
    glob patterns skip it; name it exactly in --select to include it.

    Parameters
    ----------
    harness:
        Only read these agents' memory, by agent-kit platform name (e.g.
        claude, codex, copilot). Repeatable or comma-separated. Defaults to
        every agent detected on this machine.
    repo:
        Only consider memories whose project resolves to this repo, given as a
        URI or as org/name.
    select:
        Memories to store, by the id shown in the dry run, by the memory's own
        name in its store (a file stem), or by a glob over either. Repeatable
        or comma-separated.
    all_:
        Select every memory that is not already in the graph, except user ones.
    confirm:
        Write the selected memories. Without it the command only reports.
    """
    names = _split_csv(harness) or detect_installed_platforms()
    known = known_platforms()
    if unknown := [n for n in names if n not in known]:
        print_error(
            f"--harness {', '.join(unknown)} is not an agent agent-kit knows; "
            f"choose from {', '.join(known)}."
        )
        raise SystemExit(1)
    patterns = _split_csv(select) or []
    if confirm and not (patterns or all_):
        print_error("--confirm writes only what --select or --all chooses.")
        raise SystemExit(1)

    harnesses = [local.harness(name) for name in dict.fromkeys(names)]
    found = [(h, mem) for h in harnesses for mem in h.entries]
    resolutions: dict[tuple[str, str], local.Resolution] = {}
    for h, mem in found:
        if (h.name, mem.project) not in resolutions:
            resolutions[(h.name, mem.project)] = local.resolve(mem)

    def resolution(h: local.Harness, mem: MemoryEntry) -> local.Resolution:
        return resolutions[(h.name, mem.project)]

    if repo is not None:
        found = [
            (h, mem) for h, mem in found if _matches_repo(repo, resolution(h, mem).repo)
        ]

    def by_repo(h: local.Harness, mem: MemoryEntry) -> str:
        res = resolution(h, mem)
        label = _short_repo(res.repo) if res.repo else mem.project.strip("-/")
        return f"{label}/{mem.key}"

    # A main checkout and a worktree of it resolve to one repo, and two agents
    # can name a memory alike, so one repo-based id could stand for two
    # memories and --select could not pick just one. Those fall back to the
    # agent and its own project label, which are unique.
    taken = Counter(by_repo(h, mem) for h, mem in found)
    ids = [
        by_repo(h, mem)
        if taken[by_repo(h, mem)] == 1
        else f"{h.name}:{mem.project.strip('-/')}/{mem.key}"
        for h, mem in found
    ]

    def named(i: int, pattern: str) -> bool:
        return pattern in (ids[i], found[i][1].key)

    def globbed(i: int, pattern: str) -> bool:
        return fnmatchcase(ids[i], pattern) or fnmatchcase(found[i][1].key, pattern)

    unmatched = [
        p
        for p in patterns
        if not any(named(i, p) or globbed(i, p) for i in range(len(found)))
    ]
    if unmatched:
        print_error(f"--select matched no memory: {', '.join(unmatched)}")
        raise SystemExit(1)

    from ..remote.proxy import RemoteToolFailed

    s = _srv()
    recall = _fn(s.recall)
    store = _fn(s.memory_store)
    claimed: set[tuple[str, str]] = set()
    rows: list[dict[str, object]] = []
    for i, (h, mem) in enumerate(found):
        res = resolution(h, mem)
        mem_kind = local.kind(mem)
        row: dict[str, object] = {
            "id": ids[i],
            "harness": h.name,
            "kind": mem_kind,
            "repo": res.repo,
            "status": "",
            "detail": "",
            "slug": None,
            "redaction_note": None,
            "title": mem.name,
            "path": str(mem.path),
        }
        rows.append(row)
        if mem.error or mem_kind is None:
            row.update(status="invalid", detail=mem.error or f"type {mem.type!r}")
            continue
        if res.repo is None:
            row.update(status="unresolved", detail="project has no repo")
            continue
        key = (res.repo, _norm(mem.name))
        if dup := _existing(recall, mem, res.repo):
            row.update(status="duplicate", detail=dup, slug=dup)
            continue
        if key in claimed:
            row.update(status="duplicate", detail="same title earlier in this run")
            continue
        is_user = mem.type == "user"
        chosen = any(named(i, p) for p in patterns) or (
            not is_user and (all_ or any(globbed(i, p) for p in patterns))
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
                kind=mem_kind,
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
        redacted = sorted({r["field"] for r in result.get("redactions", [])})
        row.update(
            status="stored",
            slug=result["slug"],
            detail=result["slug"]
            + (f" (redacted: {', '.join(redacted)})" if redacted else ""),
            redaction_note=result.get("redaction_note"),
        )

    render_table(
        title="Agent memories",
        columns=["id", "harness", "kind", "status", "detail", "title"],
        rows=rows,
        empty="[dim]No agent memories found.[/dim]",
        no_wrap={"harness", "kind", "status"},
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
    for h in harnesses:
        if h.missing:
            print_notice(f"[dim]{esc(h.label)}: {esc(h.missing)}.[/dim]")
    for (name, project), res in resolutions.items():
        if res.repo is None and any(
            h.name == name and mem.project == project for h, mem in found
        ):
            print_notice(
                f"[yellow]unresolved[/yellow] {esc(name)} {esc(project)}: "
                f"{esc(res.reason)}"
            )
    counts = Counter(str(r["status"]) for r in rows)
    summary = ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
    print_notice(f"{len(rows)} memories: {esc(summary or 'none')}.")
    if not confirm:
        print_notice(
            "[dim]Dry run, nothing written. Pass --confirm with --select or --all "
            "to store.[/dim]"
        )
    if counts["blocked"]:
        raise SystemExit(1)


@memory_app.command(name="stale-refs")
def stale_refs(*, repo: str | None = None, all_repos: bool = False) -> None:
    """List memories whose symbol_refs no longer resolve in the code graph.

    A review list, never an edit. ``missing`` means the repo is indexed and the
    symbol is not, which may be a rename: the detail column lists indexed
    symbols with the same name. ``unknown`` means the ref could not be checked
    (the repo is not indexed, or witan-code is unreachable).
    """
    result = _fn(_srv().memory_stale_refs)(**_repo_arg(repo, all_repos))
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
