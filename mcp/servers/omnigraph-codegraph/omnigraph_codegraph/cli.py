"""cyclopts CLI for the code indexer.

Exposed as ``omnigraph-codegraph-index`` (see pyproject [project.scripts]).
"""

from pathlib import Path

import cyclopts

from . import indexer

app = cyclopts.App(
    name="omnigraph-codegraph-index",
    help="Tree-sitter code-graph indexer (Layer 2).",
)


@app.command
def index(path: Path = Path(".")) -> None:
    """Incrementally index PATH (file or directory). Unchanged files are skipped."""
    stats = indexer.index_path(path, force=False)
    _print_summary("index", path, stats)


@app.command
def reindex(path: Path = Path(".")) -> None:
    """Force re-index PATH, ignoring content hashes."""
    stats = indexer.index_path(path, force=True)
    _print_summary("reindex", path, stats)


def _print_summary(action: str, path: Path, stats: indexer.IndexStats) -> None:
    print(
        f"{action} {path}: "
        f"scanned={stats.scanned} indexed={stats.indexed} "
        f"skipped={stats.skipped} symbols={stats.symbols} "
        f"edges={stats.edges} bindings={stats.bindings} errors={stats.errors}"
    )


def cli() -> None:
    app()


if __name__ == "__main__":
    cli()
