"""``witan journal list|replay|drop`` — writes queued while the deployment was down.

The proxy queues a task write when it cannot open a connection to the deployed
witan (``witan_core.remote.journal``) and replays the queue on the next call
that connects. These commands show what is queued, replay it on demand, and
drop an entry that should not be sent at all.

The journal belongs to a deployment, so every command resolves the selected
target the way ``_srv()`` does and refuses when there is none: a local store
never queues anything.
"""

from __future__ import annotations

import asyncio
import json

import cyclopts

from witan_core.remote.journal import WriteJournal, journal_path

from .. import config as cfg_module
from ._common import app, console, esc, print_error, remote_proxy, render_table
from .selected_target import selected_target

journal_app = cyclopts.App(
    name="journal",
    help="Inspect and replay task writes queued while the deployment was unreachable.",
)
app.command(journal_app, name="journal")


def _remote():
    try:
        remote = cfg_module.load_remote_config(target=selected_target())
    except ValueError as exc:
        print_error(exc)
        raise SystemExit(1) from None
    if remote is None:
        print_error(
            "No deployed witan is configured, so there is no journal: writes "
            "to a local store are never queued."
        )
        raise SystemExit(1)
    return remote


@journal_app.command(name="list")
def journal_list() -> None:
    """List queued writes for the selected deployment, oldest first."""
    remote = _remote()
    entries = WriteJournal(journal_path(remote.url)).entries()
    render_table(
        title=f"Queued writes — {remote.url}",
        columns=["id", "ts", "tool", "holder", "args"],
        rows=[
            {
                "id": e.id,
                "ts": e.ts,
                "tool": e.tool,
                "holder": e.holder,
                "args": json.dumps(e.args),
            }
            for e in entries
        ],
        empty="[dim]Nothing queued.[/dim]",
        no_wrap={"id", "ts", "tool"},
    )


@journal_app.command(name="replay")
def journal_replay() -> None:
    """Replay queued writes now, instead of waiting for the next call."""
    report = asyncio.run(remote_proxy(_remote()).replay_journal())
    render_table(
        title="Journal replay",
        columns=["id", "tool", "ts", "outcome", "detail"],
        rows=report,
        empty="[dim]Nothing replayed (empty, or another process is replaying).[/dim]",
        no_wrap={"id", "tool", "ts", "outcome"},
    )


@journal_app.command(name="drop")
def journal_drop(entry_id: str) -> None:
    """Remove one queued write without sending it.

    Parameters
    ----------
    entry_id: The entry's id, from ``witan journal list``.
    """
    remote = _remote()
    if not WriteJournal(journal_path(remote.url)).remove(entry_id):
        print_error(f"No queued write {entry_id!r}.")
        raise SystemExit(1)
    console.print(f"[green]Dropped[/green] [bold]{esc(entry_id)}[/bold]")
