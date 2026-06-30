"""Schema/data migrations: ``witan migrate …``."""

from __future__ import annotations

import cyclopts

from ._common import _srv, console

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


def _backfill_topics() -> None:
    s = _srv()
    if not s._topic_schema_present():
        console.print(
            "[red]The store has no Topic schema yet. "
            "Run `witan migrate schema` first (or `witan migrate all`).[/red]"
        )
        raise SystemExit(1)
    result = s.migrate_topics()
    console.print(
        f"Scanned {result['memories_scanned']} memories; "
        f"created {result['topics_created']} topics, "
        f"{result['edges_created']} tagged edges."
    )


@migrate_app.command
def schema() -> None:
    """Apply the bundled schema to the configured store (idempotent).

    Reconciles an existing store with the current schema (new nodes/edges/fields)
    — ``_ensure_graph`` only applies schema when first creating a store.
    """
    _apply_schema()


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
