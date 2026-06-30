"""Schema/data migrations: ``witan migrate …``."""

from __future__ import annotations

import cyclopts

from ._common import _srv, console

migrate_app = cyclopts.App(
    name="migrate",
    help="One-shot, idempotent data migrations.",
)


@migrate_app.command
def topics() -> None:
    """Backfill Topic nodes from existing memory tags.

    For every distinct memory ``tag``, upsert a ``Topic{kind:"topic"}`` and a
    ``Tagged`` edge. Safe to re-run — already-created topics and edges are
    skipped. Run once after deploying the Topic schema.
    """
    result = _srv().migrate_topics()
    console.print(
        f"Scanned {result['memories_scanned']} memories; "
        f"created {result['topics_created']} topics, "
        f"{result['edges_created']} tagged edges."
    )
