"""Schema/data migrations: ``witan migrate …``."""

from __future__ import annotations

from typing import Annotated, NoReturn

import cyclopts

from ._common import _srv, console, remote_proxy

migrate_app = cyclopts.App(
    name="migrate",
    help="One-shot, idempotent schema and data migrations.",
)


def _apply_schema() -> None:
    try:
        result = _srv().apply_schema()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None
    console.print(result["output"] or f"schema applied to {result['store']}")


def _repo_keys() -> None:
    s = _srv()
    try:
        result = s.migrate_repo_keys()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None
    console.print(
        f"Updated {result['tasks_updated']} task(s), {result['memories_updated']} "
        f"memory(ies), {result['sessions_updated']} session(s), "
        f"{result['projects_updated']} project(s), {result['traces_updated']} "
        f"trace(s); migrated {result['code_branches_migrated']} code branch(es)."
    )
    changed = result.get("repos_changed") or {}
    if changed:
        console.print(
            "\n[yellow]The following repos' canonical key changed case — "
            "re-run `witan-code reindex` for each (the code graph is a "
            "re-derivable cache, not covered by this migration):[/yellow]"
        )
        for old, new in changed.items():
            console.print(f"  {old} -> {new}")


def _backfill_topics() -> None:
    s = _srv()
    try:
        if not s._topic_schema_present():
            console.print(
                "[red]The store has no Topic schema yet. "
                "Run `witan migrate schema` first (or `witan migrate all`).[/red]"
            )
            raise SystemExit(1)
        result = s.migrate_topics()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None
    console.print(
        f"Scanned {result['memories_scanned']} memories; "
        f"created {result['topics_created']} topics, "
        f"{result['edges_created']} tagged edges."
    )


def _fail(message: str) -> NoReturn:
    console.print(f"[red]{message}[/red]")
    raise SystemExit(1)


def _named_target(name: str):
    """The ``[targets.<name>]`` block, or a clean exit naming what is defined."""
    from .. import config as cfg_module

    try:
        return cfg_module.load_target(name)
    except ValueError as exc:
        _fail(str(exc))


def _merge_source(source: str | None, from_target: str | None) -> str:
    """The store URI to merge from, resolving ``--from <name>`` to its ``server``."""
    from .. import config as cfg_module

    if from_target and source:
        _fail(
            "Pass a source store URI or --from <target>, not both — they name "
            "the same end of the merge."
        )
    if from_target:
        block = _named_target(from_target)
        if not block.server:
            _fail(
                f"No local store configured for target {from_target!r}: it has "
                "no `server` to export from. A target that only carries a "
                "`remote_url` cannot be a merge source — witan exports no "
                "deployment."
            )
        return cfg_module._resolve_path(block.server)
    if not source:
        _fail("Nothing to merge from: pass a source store URI, or --from <target>.")
    return source


def _merge_destination(to_target: str | None, target: str | None):
    """The ``(provider, target URI)`` pair the merge writes through.

    Without ``--to`` this is the ambient resolution every other command uses
    (``_srv()``, picked from ``WITAN_TARGET``/``match_*`` before any flag is
    read) and ``--target``'s literal URI, if given. With ``--to <name>`` the
    destination is the named block instead: its deployment's own proxy when it
    carries a ``remote_url`` — the same object ``WITAN_TARGET=<name>`` would
    have produced, chosen on the command line rather than out of the
    environment — or its ``server`` as a plain target URI when it does not.
    """
    from .. import config as cfg_module

    if to_target and target:
        _fail(
            "--to names a configured target and --target a store URI; both name "
            "the destination, so pass one or the other."
        )
    if not to_target:
        return _srv(), target

    block = _named_target(to_target)
    if block.remote_url:
        try:
            remote = cfg_module.load_remote_config(target=to_target)
        except ValueError as exc:
            _fail(str(exc))
        return remote_proxy(remote), None
    if block.server:
        from .. import server as server_module

        return server_module, cfg_module._resolve_path(block.server)
    _fail(
        f"Target {to_target!r} configures neither `remote_url` nor `server`, so "
        "there is nothing to merge into. Give it one, or pass --target <uri>."
    )


def _merge(
    source: str | None,
    target: str | None,
    dry_run: bool,
    from_target: str | None = None,
    to_target: str | None = None,
) -> None:
    source = _merge_source(source, from_target)
    s, target = _merge_destination(to_target, target)
    try:
        result = s.merge_store(source, target=target, dry_run=dry_run)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None

    if dry_run:
        console.print(f"[yellow]Dry run[/yellow] against {result['target']}:")
        for d in result["decisions"]:
            console.print(f"  {d['decision']:12} {d['type']:16} {d['slug']}")
        console.print(
            f"{result['added']} to add, {result['updated']} to update, "
            f"{result['kept_target']} kept (target already newer-or-equal)."
        )
        return

    console.print(
        f"[green]Merged[/green] {source} into {result['target']}: "
        f"{result['added']} added, {result['updated']} updated, "
        f"{result['kept_target']} kept (target already newer-or-equal), "
        f"{result['rows_loaded']} rows loaded."
    )


def _migrate_storage(old_binary: str | None, yes: bool) -> None:
    s = _srv()
    store = s.client.graph_uri
    if not yes:
        console.print(
            f"[yellow]About to rebuild {store} onto the current omnigraph "
            "on-disk format. Commit history and branches are dropped; the "
            "original is kept as a `.pre-migrate` backup, not deleted.[/yellow]"
        )
        try:
            response = input("Continue? [y/N] ").strip().lower()
        except EOFError:
            console.print(
                "\n[red]Aborted (non-interactive terminal; pass --yes to skip the prompt).[/red]"
            )
            raise SystemExit(1) from None
        if response not in ("y", "yes"):
            console.print("Aborted.")
            raise SystemExit(1)
    try:
        result = s.migrate_storage_format(old_binary)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None
    if not result["migrated"]:
        console.print(result["reason"])
        return
    console.print(
        f"[green]Migrated[/green] {result['store']} "
        f"(old binary: {result['old_binary']}, backup: {result['backup']})."
    )
    console.print(result["verify"])


@migrate_app.command
def schema() -> None:
    """Apply the bundled schema to the configured store (idempotent).

    Reconciles an existing store with the current schema (new nodes/edges/fields).
    Startup now does this on its own when ``schema.pg`` changes; this forces the
    apply regardless of the mtime stamp.
    """
    _apply_schema()


@migrate_app.command
def storage(
    old_binary: str | None = None,
    *,
    yes: bool = False,
) -> None:
    """Rebuild a local store stuck on an old, incompatible omnigraph format.

    omnigraph uses strict single-version storage: a release that bumps the
    internal on-disk schema (e.g. 0.7 → 0.8) refuses to open graphs an older
    binary wrote. This detects that refusal against your configured store
    and, using a still-installed pre-upgrade ``omnigraph`` binary, replays
    the documented rebuild — export with the old binary, then ``init`` +
    ``load`` with the new one. Node/edge data, vectors, and blobs are
    preserved; commit history and branches are not. The original store is
    renamed ``<store>.pre-migrate`` rather than deleted.

    No-op if the store already opens fine with the current binary. Only
    handles local on-disk stores — s3:// and http(s):// stores are managed
    externally and must be rebuilt by hand per omnigraph's upgrade docs.

    Parameters
    ----------
    old_binary:
        Path to the omnigraph binary that last wrote this store. Auto-detected
        as the first ``omnigraph`` on PATH that isn't the one witan is
        currently using, if omitted.
    yes:
        Skip the confirmation prompt.
    """
    _migrate_storage(old_binary, yes)


@migrate_app.command
def merge(
    source: str | None = None,
    *,
    from_: Annotated[str | None, cyclopts.Parameter(name="--from")] = None,
    to: Annotated[str | None, cyclopts.Parameter(name="--to")] = None,
    target: str | None = None,
    dry_run: bool = False,
) -> None:
    """Merge another store's data into this store, newest-record-wins on collisions.

    Implements docs/migration-runbook.md's export -> reconcile -> load
    (--mode merge) path: for every node present in both stores (same type +
    slug), keeps whichever has the newer timestamp instead of `omnigraph load
    --mode merge`'s raw last-loaded-wins overwrite, which ignores content
    entirely. Rows only in ``source`` are always added; rows only in the
    target are left untouched. Repeatable — re-running against an
    already-merged target loads nothing new.

    Parameters
    ----------
    source:
        Store URI to merge from (local path, ``s3://``, ``file://``, or an
        ``http(s)://`` omnigraph-server), or the path to a *local*
        ``omnigraph export`` JSONL — anything ending ``.jsonl`` is read as an
        export rather than re-exported, and is never fetched remotely. Use the
        export form to merge a store from another machine: Lance embeds
        absolute paths, so a ``.omni`` directory cannot be copied, but its
        export can.
    from_:
        Named ``[targets.<name>]`` block to merge *from*, in place of
        ``source`` — its ``server`` is the store URI. A target carrying only a
        ``remote_url`` is refused: there is no remote-export path, so it has
        nothing to merge from.
    to:
        Named ``[targets.<name>]`` block to merge *into*, in place of the
        ambient destination. Spells out on the command line what setting
        ``WITAN_TARGET`` does out of the environment: a target with a
        ``remote_url`` is merged into through that deployment (as you, over
        MCP), one with only a ``server`` into that store URI. Mutually
        exclusive with ``target``, which names a store rather than a target.
    target:
        Store URI to merge into. Defaults to the configured store. Created
        automatically if it's a local path that doesn't exist yet. A deployed
        graph is ``http(s)://<host>:<port>/graphs/<graph-id>`` (or just the
        configured store, when running in-cluster). Unlike ``source``, a
        ``.jsonl`` target is refused rather than treated as a store: merging
        appends to a graph, and an export is a snapshot of one.
    dry_run:
        Preview the reconciliation decision for every colliding slug without
        writing anything.
    """
    _merge(source, target, dry_run, from_target=from_, to_target=to)


@migrate_app.command
def topics() -> None:
    """Backfill Topic nodes from existing memory tags.

    For every distinct memory ``tag``, upsert a ``Topic{kind:"topic"}`` and a
    ``Tagged`` edge. Safe to re-run — already-created topics and edges are
    skipped. Fails fast if the Topic schema isn't applied yet.
    """
    _backfill_topics()


@migrate_app.command(name="repo-keys")
def repo_keys() -> None:
    """Fold every stored repo key onto its canonical, case-folded form (#142).

    ``normalise`` now lowercases GitHub/GitLab repo keys, so a key written
    before that fix may still carry the old case and silently drop out of
    every repo-scoped read. Rewrites Task/Memory/WorkflowSession ``repo`` (and
    their ``symbol_refs`` repo prefixes), WorkflowProject/WorkflowTrace
    ``repos`` lists, and CodeBranch (recreated under the canonical slug, the
    stale row marked ``abandoned``). Idempotent — safe to re-run, and safe to
    run on a store with nothing to fix. Does not touch the code graph
    (witan-code); prints which repos need `witan-code reindex` instead.
    """
    _repo_keys()


@migrate_app.command(name="dedupe-sessions")
def dedupe_sessions(
    *,
    apply: bool = False,
    supersede: list[str] | None = None,
) -> None:
    """Flag WorkflowSessions a pre-upsert ``workflow_session_start`` duplicated.

    Reports overlapping sessions that share a ``session_id`` — the signature of
    a hook retry or transport reconnect — and marks the ones carrying no
    summary as ``superseded_by`` the surviving session, so trace assembly and
    the context hook's counts stop double-counting them. Nothing is deleted.

    Dry by default: prints what it would do and changes nothing until
    ``--apply``. Sessions that share a ``session_id`` but ran one after another
    are left alone — one session id legitimately spans several working stints.
    Runs where every member wrote a real summary are reported rather than
    guessed at; resolve those with ``--supersede``.

    Deliberately not part of ``migrate all``: unlike the other migrations this
    one makes a judgment call about corpus content, so it should be read before
    it's applied.

    Parameters
    ----------
    apply:
        Write the marks instead of only reporting them.
    supersede:
        ``<duplicate-slug>=<survivor-slug>`` pairs to mark regardless of the
        automatic rule. Repeatable.
    """
    extra: dict[str, str] = {}
    for pair in supersede or []:
        dup, sep, survivor = pair.partition("=")
        if not sep or not dup.strip() or not survivor.strip():
            console.print(
                f"[red]--supersede expects <duplicate-slug>=<survivor-slug>, got {pair!r}[/red]"
            )
            raise SystemExit(1)
        extra[dup.strip()] = survivor.strip()

    try:
        result = _srv().migrate_dedupe_sessions(apply=apply, extra_marks=extra or None)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None

    marked = result["marked"]
    console.print(f"Scanned {result['sessions_scanned']} session(s).")
    if marked:
        verb = "Marked" if result["applied"] else "[yellow]Would mark[/yellow]"
        console.print(f"{verb} {len(marked)} duplicate session(s):")
        for dup, survivor in marked.items():
            console.print(f"  {dup} -> {survivor}")
    else:
        console.print("No duplicate sessions to mark.")

    for run in result["needs_review"]:
        console.print(
            f"\n[yellow]Needs review[/yellow] — {run['project_slug']} / "
            f"{run['session_id']}: overlapping sessions that each wrote a real "
            "summary. Resolve with --supersede <dup>=<survivor> if they are "
            "in fact one session:"
        )
        for sess in run["sessions"]:
            console.print(f"  {sess['slug']}  {sess['started_at']}")
            console.print(f"    {sess['summary']}")

    if result["sealed_traces"]:
        console.print(
            "\n[yellow]These projects already have a sealed WorkflowTrace, whose "
            "session_count was computed before the marks above and is immutable "
            "by design — the trace stays over-counted:[/yellow]"
        )
        for project_slug in result["sealed_traces"]:
            console.print(f"  {project_slug}")

    if marked and not result["applied"]:
        console.print("\n[dim]Dry run — re-run with --apply to write.[/dim]")


@migrate_app.command(name="all")
def all_() -> None:
    """Run the full bring-up: apply schema, backfill topics, fold repo keys.

    All three steps are idempotent, so this is safe to re-run — including as
    part of every deploy, to keep a live store self-healing.
    """
    _apply_schema()
    _backfill_topics()
    _repo_keys()
