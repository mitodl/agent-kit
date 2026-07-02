"""Schema/data migrations: ``witan migrate …``."""

from __future__ import annotations

import cyclopts

from ._common import _srv, console

migrate_app = cyclopts.App(
    name="migrate",
    help="One-shot, idempotent schema and data migrations.",
)


def _print_error(exc: Exception) -> None:
    console.print(f"[red]{exc}[/red]")
    if _srv()._is_storage_version_mismatch(str(exc)):
        console.print(
            "[yellow]This looks like an incompatible on-disk storage "
            "upgrade (e.g. omnigraph 0.7 → 0.8). Run "
            "`witan migrate storage` first.[/yellow]"
        )


def _apply_schema() -> None:
    try:
        result = _srv().apply_schema()
    except RuntimeError as exc:
        _print_error(exc)
        raise SystemExit(1) from None
    console.print(result["output"] or f"schema applied to {result['store']}")


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
        _print_error(exc)
        raise SystemExit(1) from None
    console.print(
        f"Scanned {result['memories_scanned']} memories; "
        f"created {result['topics_created']} topics, "
        f"{result['edges_created']} tagged edges."
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

    Reconciles an existing store with the current schema (new nodes/edges/fields)
    — ``_ensure_graph`` only applies schema when first creating a store.
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
def topics() -> None:
    """Backfill Topic nodes from existing memory tags.

    For every distinct memory ``tag``, upsert a ``Topic{kind:"topic"}`` and a
    ``Tagged`` edge. Safe to re-run — already-created topics and edges are
    skipped. Fails fast if the Topic schema isn't applied yet.
    """
    _backfill_topics()


@migrate_app.command(name="all")
def all_() -> None:
    """Run the full bring-up: apply schema, then backfill topics.

    Both steps are idempotent, so this is safe to re-run.
    """
    _apply_schema()
    _backfill_topics()
