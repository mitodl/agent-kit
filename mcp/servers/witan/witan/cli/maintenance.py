"""Maintenance commands: optimize, cleanup.

Wrappers over ``omnigraph optimize`` / ``cleanup`` for cron / systemd-timer
driven store compaction. The Stop hook also spawns ``witan optimize`` in the
background opportunistically (see ``witan.maintenance``), but a scheduled
``witan optimize`` (and an occasional ``witan cleanup`` to reclaim disk) is the
robust path for a busy shared store.
"""

from __future__ import annotations

from pathlib import Path

from .. import config as cfg_module
from ..graph import OmnigraphClient
from ._common import app, console

_REMOTE_PREFIXES = ("http://", "https://", "s3://")


def _resolve_store(store: str | None) -> str | None:
    """Resolve the store URI (explicit or from config); None if a local store
    file is missing (nothing to compact).

    Expands ``~`` for a local ``--store`` path so a user-supplied ``~/…`` isn't
    treated as missing (config paths are already expanded by the config loader).
    """
    cfg = cfg_module.load()
    graph_uri = store or cfg.graph_uri
    if not graph_uri.startswith(_REMOTE_PREFIXES):
        graph_uri = str(Path(graph_uri).expanduser())
        if not Path(graph_uri).exists():
            console.print(f"[dim]No store at {graph_uri} — nothing to do.[/dim]")
            return None
    return graph_uri


def _client(graph_uri: str) -> OmnigraphClient:
    cfg = cfg_module.load()
    return OmnigraphClient(graph_uri, cfg.queries_dir, cfg.graph_token)


@app.command
def optimize(*, store: str | None = None) -> None:
    """Compact the graph store's Lance fragments (non-destructive).

    Collapses the many tiny fragments that accrue from every write so opening
    the store stays cheap. Safe to run repeatedly; takes the store write lock.

    Parameters
    ----------
    store: Store URI to optimize (default: the configured graph store).
    """
    graph_uri = _resolve_store(store)
    if graph_uri is None:
        return
    console.print(f"[dim]Optimizing {graph_uri} …[/dim]")
    _client(graph_uri).optimize()
    console.print("[green]Optimized.[/green] (run `witan cleanup` to reclaim disk)")


@app.command
def cleanup(
    *,
    store: str | None = None,
    keep: int = 10,
    older_than: str | None = None,
    yes: bool = False,
) -> None:
    """Remove old Lance versions to reclaim disk (**destructive**).

    ``optimize`` compacts fragments but leaves old versions behind; this GCs
    them, keeping the most recent ``keep`` versions per table (and/or those
    newer than ``older_than``). Irreversible, so it requires ``--yes``.

    Parameters
    ----------
    store: Store URI to clean (default: the configured graph store).
    keep: Number of recent versions to keep per table.
    older_than: Also keep versions newer than this Go-style duration (e.g. 7d).
    yes: Confirm the destructive operation (required to actually run).
    """
    graph_uri = _resolve_store(store)
    if graph_uri is None:
        return
    if not yes:
        console.print(
            f"[yellow]cleanup is destructive[/yellow] — would keep the {keep} most "
            f"recent version(s) per table"
            + (f" and anything newer than {older_than}" if older_than else "")
            + f" in {graph_uri}.\nRe-run with [bold]--yes[/bold] to proceed."
        )
        return
    console.print(f"[dim]Cleaning up {graph_uri} (keep={keep}) …[/dim]")
    _client(graph_uri).cleanup(keep=keep, older_than=older_than)
    console.print("[green]Cleaned up.[/green]")
