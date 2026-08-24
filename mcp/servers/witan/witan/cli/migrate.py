"""Schema/data migrations: ``witan migrate …``."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, NoReturn

import cyclopts

from .. import merge_watermark
from ._common import _srv, console, esc, print_error, remote_proxy

migrate_app = cyclopts.App(
    name="migrate",
    help="One-shot, idempotent schema and data migrations.",
)


def _apply_schema() -> None:
    try:
        result = _srv().apply_schema()
    except RuntimeError as exc:
        print_error(exc)
        raise SystemExit(1) from None
    console.print(esc(result["output"] or f"schema applied to {result['store']}"))


def _repo_keys() -> None:
    s = _srv()
    try:
        result = s.migrate_repo_keys()
    except RuntimeError as exc:
        print_error(exc)
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
            console.print(f"  {esc(old)} -> {esc(new)}")


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
        print_error(exc)
        raise SystemExit(1) from None
    console.print(
        f"Scanned {result['memories_scanned']} memories; "
        f"created {result['topics_created']} topics, "
        f"{result['edges_created']} tagged edges."
    )


def _fail(message: str) -> NoReturn:
    print_error(message)
    raise SystemExit(1)


def _named_target(name: str):
    """The ``[targets.<name>]`` block, or a clean exit naming what is defined."""
    from .. import config as cfg_module

    try:
        return cfg_module.load_target(name)
    except ValueError as exc:
        _fail(str(exc))


def _target_store_uri(name: str, block) -> str:
    """The store URI a named target addresses, spelled the way a merge needs it.

    Not simply the block's ``server``. Downstream, ``merge_store`` addresses a
    store it holds no client for (``store_cli_args``), so anything the target
    keeps in a *separate* key has to be folded in here or it is lost:

    - ``graph`` is a sibling key, and a bare ``http(s)://host:port`` with no
      ``/graphs/<id>`` is rejected as having no graph id. Folded into the URI,
      which is the spelling ``store_cli_args`` already reads.
    - ``token`` has nowhere to go — the merge path takes a per-store credential
      only from the *configured* client, and falls back to an ambient
      ``OMNIGRAPH_BEARER_TOKEN`` for any other remote store. Authenticating to
      somebody's server with whatever token happens to be exported is worse
      than refusing, so this refuses.

    ``file://`` is stripped here rather than left to ``config._resolve_path``,
    whose ``Path()`` round-trip collapses the ``//`` and yields a relative path
    named ``file:``.
    """
    server = block.server
    if server.startswith("file://"):
        return server[len("file://") :]
    if not server.startswith(("http://", "https://")):
        return server if server.startswith("s3://") else str(Path(server).expanduser())
    if block.token:
        _fail(
            f"Target {name!r} declares a `token` for its remote store, and the "
            "merge path has no way to carry a per-store credential. Export it "
            "as OMNIGRAPH_BEARER_TOKEN and name the store directly instead."
        )
    base = server.rstrip("/")
    if "/graphs/" in base:
        return base
    return f"{base}/graphs/{block.graph}" if block.graph else base


def _merge_source_author(from_target: str | None) -> str | None:
    """The author string the SOURCE store writes — not the ambient target's.

    `--from <name>` merges a store the ambient configuration is not pointed at,
    and a target block can carry its own `author`. Resolving this from ambient
    config would send the wrong name, and since `store_merge` restamps only
    rows matching it, the effect is silent: nothing is claimed and #267 stays
    reproducible for exactly the caller who used `--from`.

    Falls back to ambient for a bare source URI, which is the headline cutover
    (`witan migrate merge ~/.local/share/witan/graph.omni --to ol`) — there the
    source is this machine's own store and ambient IS its author.
    """
    from .. import config as cfg_module

    if from_target:
        block = _named_target(from_target)
        if block.author:
            return block.author
    return cfg_module.load().author


def _merge_source(source: str | None, from_target: str | None) -> str:
    """The store URI to merge from, resolving ``--from <name>`` to its ``server``."""
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
        return _target_store_uri(from_target, block)
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

        return server_module, _target_store_uri(to_target, block)
    _fail(
        f"Target {to_target!r} configures neither `remote_url` nor `server`, so "
        "there is nothing to merge into. Give it one, or pass --target <uri>."
    )


def _destination_key(provider, target: str | None) -> str:
    """How a merge destination is named in the watermark file.

    Computed from what is known BEFORE the merge — the watermark has to be read
    to be passed in — and used again to write, so the two can't disagree. That
    rules out ``result["target"]``, which is the same address after the callee
    has normalized it.
    """
    remote_url = getattr(provider, "remote_url", None)
    if remote_url:
        return remote_url
    if target:
        return target
    return provider.client.graph_uri


def _since_marks(entry: dict | None) -> dict | None:
    """Just the two timestamps out of a stored watermark entry."""
    if not entry:
        return None
    return {"source_ts": entry.get("source_ts"), "target_ts": entry.get("target_ts")}


def _report_divergence(result: dict, since: dict | None, dry_run: bool) -> None:
    """Name the nodes both stores wrote, which reconciliation resolves by
    discarding one side's edit.

    The report is the whole feature. A discarded divergent edit is otherwise
    indistinguishable in the summary from a node that genuinely needed no
    action — it lands in ``kept``, which reads as "nothing to do" — and several
    witan fields (``WorkflowProject.description`` above all) are append-only
    logs where losing one side loses real content.
    """
    if not since:
        # Only worth saying when something actually collided. A merge that only
        # adds has nothing a watermark could have told us about, and the note
        # would just be noise on the run that needs it least.
        if result["updated"] or result["kept_target"]:
            # A dry run records nothing, so the NEXT run is blind too — the
            # earliest run that can report is the one after the next real
            # merge. Promising otherwise here would have someone read the
            # following merge's silence as "nothing diverged".
            next_step = (
                "A dry run records none, so the real merge after this one is "
                "blind as well; the one after that can report."
                if dry_run
                else "Recorded after this merge; the next one will report divergence."
            )
            console.print(
                "[dim]No merge watermark for this pair yet, so nothing can be "
                f"said about the collisions above. {next_step}[/dim]"
            )
        return
    diverged = [d for d in result["decisions"] if d.get("diverged")]
    if not diverged:
        return
    # Tense matters here. Before a dry run there is still a choice to make;
    # after a real merge the losing edit is already gone from the target and
    # the remedy is to put it back, not to reconsider.
    consequence = (
        "Newest-record-wins will keep one side and drop the other's edit. "
        "Reconcile these before you merge for real"
        if dry_run
        else "Newest-record-wins kept one side and DROPPED the other's edit. "
        "The losing text is still in the store that lost; put the combined "
        "value back by hand"
    )
    console.print(
        f"\n[yellow]{len(diverged)} node(s) changed on BOTH sides since the last "
        f"merge[/yellow] (watermark {esc(str(since.get('merged_at')))}). "
        f"{consequence} — for an append-only field (a WorkflowProject "
        "description) this is lost content, not a stale value:"
    )
    for d in diverged:
        kept = "source" if d["decision"] == "updated" else "target"
        # `source_at`/`target_at` ahead of the raw `source_ts`/`target_ts`: an
        # omnigraph >= 0.9 export spells a timestamp as epoch millis, and two
        # 13-digit integers are not something to eyeball against each other.
        source_at = d.get("source_at") or d["source_ts"]
        target_at = d.get("target_at") or d["target_ts"]
        console.print(
            f"  {d['type']:16} {esc(d['slug'])}\n"
            f"    source {esc(str(source_at))}  target {esc(str(target_at))}"
            f"  -> kept {kept}"
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
    key = _destination_key(s, target)
    since = merge_watermark.read(source, key)
    # Held in memory for THIS run's report, then retired from the file before
    # the first batch commits — see `_invalidate_watermark`.
    if not dry_run:
        _invalidate_watermark(source, key)
    try:
        result = s.merge_store(
            source,
            target=target,
            dry_run=dry_run,
            source_author=_merge_source_author(from_target),
            # The two marks only. The stored entry also carries which pair it
            # describes and when it was taken, which is this machine's business
            # — a merge sends the deployment its rows, not the local path they
            # came from.
            since=_since_marks(since),
        )
    except RuntimeError as exc:
        print_error(exc)
        raise SystemExit(1) from None

    if dry_run:
        console.print(f"[yellow]Dry run[/yellow] against {result['target']}:")
        for d in result["decisions"]:
            console.print(f"  {d['decision']:12} {d['type']:16} {esc(d['slug'])}")
        console.print(
            f"{result['added']} to add, {result['updated']} to update, "
            f"{result['kept_target']} kept (target already newer-or-equal)."
        )
        # Deliberately not recorded: the watermark describes a graph with this
        # merge's winners in it, and a dry run wrote none of them. Storing it
        # would tell the next run that everything up to here had already been
        # merged, when nothing had.
        _report_divergence(result, since, dry_run=True)
        return

    console.print(
        f"[green]Merged[/green] {source} into {result['target']}: "
        f"{result['added']} added, {result['updated']} updated, "
        f"{result['kept_target']} kept (target already newer-or-equal), "
        f"{result['rows_loaded']} rows loaded."
    )
    _report_divergence(result, since, dry_run=False)
    _record_watermark(source, key, result)


def _invalidate_watermark(source: str, key: str) -> None:
    """Drop the standing mark before a real merge writes anything.

    A merge is not atomic — its batches commit independently — so a run that
    dies part-way leaves rows in the target that the standing mark predates.
    Left in place, the next run reads exactly those rows as an independent
    target edit and reports divergence on rows nothing but the failed merge
    ever wrote (reproduced against `_reconcile_nodes` before this was added).

    So the old mark is retired first and the new one installed only on success.
    A crashed merge then leaves no mark, and the next run says it cannot tell —
    which is true of a graph whose last write was half a merge.
    """
    merge_watermark.forget(source, key)


def _record_watermark(source: str, key: str, result: dict) -> None:
    """Store the mark this merge just established, and report honestly when it
    could not.

    The old mark is already gone by now (`_invalidate_watermark`), so every path
    out of here that does not write one leaves the pair unmarked. That is the
    safe direction — the next run says it cannot tell rather than measuring
    against a mark that predates rows already in the target — but it is never
    silent unless there was genuinely nothing to mark.
    """
    watermark = result.get("watermark")
    # A merge that carried no rows at all reports no mark either, and there is
    # nothing wrong with that — warning would send someone looking for a
    # deployment problem that is not there.
    if not result["decisions"] and not result["rows_loaded"]:
        return
    if not merge_watermark.is_usable(watermark):
        # Either an older deployment that returns no mark, or one missing a
        # side. Both are unusable, and both leave the next merge blind, so they
        # get one message rather than a distinction the reader cannot act on.
        console.print(
            "[yellow]No usable merge watermark came back, so the next merge "
            "cannot report divergence. Check the deployment is on witan-council "
            "0.29.0 or later; until then, diff the projects you care about by "
            "hand before merging again.[/yellow]"
        )
        return
    if not merge_watermark.write(source, key, watermark):
        console.print(
            f"[yellow]Could not write {esc(str(merge_watermark.path()))} — the "
            "merge itself is unaffected, but the next one will not be able to "
            "report divergence.[/yellow]"
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
        print_error(exc)
        raise SystemExit(1) from None
    if not result["migrated"]:
        console.print(esc(result["reason"]))
        return
    console.print(
        f"[green]Migrated[/green] {result['store']} "
        f"(old binary: {result['old_binary']}, backup: {result['backup']})."
    )
    console.print(esc(result["verify"]))


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

    Each merge records a per-side watermark for the pair of stores, so the next
    one can name the nodes BOTH sides have written since — the case where
    newest-record-wins is not resolving a stale value but discarding somebody's
    edit. Nothing is auto-merged; the divergent slugs are reported for you to
    reconcile. The first merge of a pair has no watermark and says so.

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
        print_error(exc)
        raise SystemExit(1) from None

    marked = result["marked"]
    console.print(f"Scanned {result['sessions_scanned']} session(s).")
    if marked:
        verb = "Marked" if result["applied"] else "[yellow]Would mark[/yellow]"
        console.print(f"{verb} {len(marked)} duplicate session(s):")
        for dup, survivor in marked.items():
            console.print(f"  {esc(dup)} -> {esc(survivor)}")
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
            console.print(f"  {sess['slug']}  {esc(sess['started_at'])}")
            console.print(f"    {esc(sess['summary'])}")

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


@migrate_app.command(name="claim-authorship")
def claim_authorship(
    was: str | None = None,
    *,
    apply: bool = False,
) -> None:
    """Take ownership of rows an earlier migration left under your local name.

    A local store writes ``author`` from ``WITAN_AUTHOR`` / git ``user.name`` /
    ``$USER``; a deployment resolves it from your token's
    ``preferred_username``. The two never converge, so before this was fixed
    every row you migrated kept a name your deployed identity cannot match —
    and ``memory_delete`` refuses anyone but the author, permanently (#267).

    ``witan migrate merge`` now claims rows as they arrive, so this is only
    needed for a store merged before that landed. Re-merging will not fix
    those: reconciliation is newest-record-wins, and a re-sent row loses to its
    own already-applied copy.

    Dry by default. Run ``witan whoami`` first if you are unsure which identity
    you are claiming *to*.

    Parameters
    ----------
    was:
        The author string the rows currently carry. Defaults to this machine's
        configured local author, which is the right answer when you are
        repairing your own cutover from this same checkout.
    apply:
        Write the change instead of only reporting it.
    """
    from .. import config as cfg_module

    was = was or cfg_module.load().author
    try:
        result = _srv().claim_authorship(was=was, apply=apply)
    except RuntimeError as exc:
        print_error(exc)
        raise SystemExit(1) from None

    # `was`/`now` are stored graph content (an author string), so they go
    # through `esc` like every other renderer — an author carrying brackets
    # would otherwise print with that substring silently dropped, in a message
    # whose entire job is to show you which identity is which.
    if result.get("reason"):
        console.print(f"Nothing to do: {esc(result['reason'])}.")
        return

    if not result["claimed"]:
        console.print(
            f"No rows authored by {esc(repr(result['was']))}. "
            f"You are {esc(repr(result['now']))} here — check `witan whoami` "
            "and the author the source store actually wrote."
        )
        return

    verb = "Claimed" if result["applied"] else "[yellow]Would claim[/yellow]"
    console.print(
        f"{verb} {result['claimed']} row(s): "
        f"{esc(repr(result['was']))} -> {esc(repr(result['now']))}"
    )
    for node_type, count in result["by_type"].items():
        console.print(f"  {node_type:16} {count}")

    if not result["applied"]:
        console.print("\n[dim]Dry run — re-run with --apply to write.[/dim]")
