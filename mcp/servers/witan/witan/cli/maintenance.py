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
from .. import maintenance
from ..graph import OmnigraphClient
from ._common import app, console

_SERVER_PREFIXES = ("http://", "https://")
_REMOTE_PREFIXES = (*_SERVER_PREFIXES, "s3://")


def _resolve_store(store: str | None) -> str | None:
    """Resolve the store URI (explicit or from config); None if a local store
    file is missing (nothing to compact).

    Expands ``~`` for a local ``--store`` path so a user-supplied ``~/…`` isn't
    treated as missing (config paths are already expanded by the config loader).
    """
    cfg = cfg_module.load()
    graph_uri = store or cfg.graph_uri
    if graph_uri.startswith(_SERVER_PREFIXES):
        console.print(
            f"{graph_uri} is a deployed graph. `optimize` and `cleanup` are "
            "direct-storage commands (omnigraph rejects `--server` for them), "
            "and from omnigraph 0.12 they are refused while the server holds "
            "the cluster's `serve` lock. The data tier's maintenance job runs "
            "them against the storage root in a stop window. Nothing to do here."
        )
        return None
    if not graph_uri.startswith(_REMOTE_PREFIXES):
        graph_uri = str(Path(graph_uri).expanduser())
        if not Path(graph_uri).exists():
            console.print(f"[dim]No store at {graph_uri} — nothing to do.[/dim]")
            return None
    return graph_uri


def _client(graph_uri: str) -> OmnigraphClient:
    cfg = cfg_module.load()
    return OmnigraphClient(
        graph_uri,
        cfg.queries_dir,
        cfg.graph_token,
        graph_id=cfg.graph_name,
        s3_profile=cfg.s3_profile,
        s3_region=cfg.s3_region,
    )


@app.command
def optimize(
    *, store: str | None = None, cleanup_older_than: str | None = None
) -> None:
    """Compact the graph store's Lance fragments, optionally then cleaning up.

    Collapses the many tiny fragments that accrue from every write so opening
    the store stays cheap. Safe to run repeatedly; takes the store write lock.

    Non-destructive on its own. ``--cleanup-older-than`` adds a destructive
    ``cleanup`` with no ``--yes`` prompt, because the Stop hook is what passes
    it. The cleanup only runs if the optimize succeeded, and the hook has
    already stamped both throttles, so a failing optimize also defers the
    cleanup to the next cleanup window.

    Parameters
    ----------
    store
        Store URI to optimize (default: the configured graph store).
    cleanup_older_than
        Then run ``cleanup`` removing versions older than this
        Go-style duration (e.g. 30d). Destructive; the Stop hook passes it on a
        slower cadence than optimize itself.
    """
    graph_uri = _resolve_store(store)
    if graph_uri is None:
        return
    console.print(f"[dim]Optimizing {graph_uri} …[/dim]")
    client = _client(graph_uri)
    client.optimize()
    if cleanup_older_than is None:
        console.print("[green]Optimized.[/green] (run `witan cleanup` to reclaim disk)")
        return
    console.print(f"[dim]Cleaning up versions older than {cleanup_older_than} …[/dim]")
    client.cleanup(older_than=cleanup_older_than)
    console.print("[green]Optimized and cleaned up.[/green]")


@app.command
def cleanup(
    *,
    store: str | None = None,
    keep: int | None = None,
    older_than: str | None = None,
    yes: bool = False,
) -> None:
    """Remove old Lance versions to reclaim disk (**destructive**).

    ``optimize`` compacts fragments but leaves old versions behind; this GCs
    the ones no retained graph commit pins. A commit is retained when either
    bound keeps it. From omnigraph 0.11 it is also the only thing that
    reclaims the storage of deleted branches. Irreversible, so it requires
    ``--yes``.

    With neither bound given it keeps the last 30 days, the same policy the
    Stop hook and the deployed maintenance job use.

    Parameters
    ----------
    store
        Store URI to clean (default: the configured graph store).
    keep
        Keep the newest N graph commits of every live branch, whatever
        their age. (omnigraph 0.11 counted versions per table instead.)
    older_than
        Keep every graph commit newer than this Go-style duration (e.g. 7d).
    yes
        Confirm the destructive operation (required to actually run).
    """
    graph_uri = _resolve_store(store)
    if graph_uri is None:
        return
    if keep is None and older_than is None:
        older_than = maintenance.CLEANUP_OLDER_THAN
    kept = []
    if keep is not None:
        kept.append(
            f"the {keep} newest graph commit(s) of every live branch "
            "(version(s) per table on omnigraph 0.11)"
        )
    if older_than is not None:
        kept.append(f"every graph commit newer than {older_than}")
    policy = " and ".join(kept)
    if not yes:
        console.print(
            f"[yellow]cleanup is destructive[/yellow] — would keep {policy} "
            f"in {graph_uri}.\nRe-run with [bold]--yes[/bold] to proceed."
        )
        return
    console.print(f"[dim]Cleaning up {graph_uri} (keeping {policy}) …[/dim]")
    _client(graph_uri).cleanup(keep=keep, older_than=older_than)
    console.print("[green]Cleaned up.[/green]")
