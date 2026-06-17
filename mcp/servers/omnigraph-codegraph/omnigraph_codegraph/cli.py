"""cyclopts CLI for the code indexer.

Exposed as ``omnigraph-codegraph-index`` (see pyproject [project.scripts]).
"""

from pathlib import Path
from typing import Literal

import cyclopts

from . import indexer

app = cyclopts.App(
    name="omnigraph-codegraph-index",
    help="Tree-sitter code-graph indexer (Layer 2).",
)

BindingKind = Literal["env_var", "package", "service", "endpoint"]


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


@app.command
def deps(
    kind: BindingKind | None = None,
    repo: str | None = None,
    html: Path | None = None,
    open_browser: bool = False,
) -> None:
    """Visualize cross-repo dependencies from the shared bridge store.

    Prints a Rich summary of "repo A depends on repo B" links (A consumes a
    contract B provides). Pass --html PATH to also emit an interactive graph.

    Parameters
    ----------
    kind:
        Filter to one contract kind (env_var/package/service/endpoint).
    repo:
        Keep only links touching a repo whose slug contains this substring.
    html:
        Write a self-contained interactive HTML graph to this path.
    open_browser:
        Open the generated HTML in the default browser.
    """
    from . import config as cfg_module
    from . import store as store_module
    from . import visualize
    from .graph import OmnigraphClient

    cfg = cfg_module.load()
    store = store_module.bridge_store(cfg)
    if not store.exists():
        print(
            "No bridge store yet — run `omnigraph-codegraph-index index` in your "
            "repos first."
        )
        return

    client = OmnigraphClient(str(store), cfg.queries_dir)
    rows = client.read("bridge.gq", "all_bindings", {})
    graph = visualize.build_graph(rows, kind=kind, repo=repo)
    visualize.render_rich(graph)

    if html is not None:
        out = visualize.render_html(graph, html)
        print(f"\nwrote {out}")
        if open_browser:
            import webbrowser

            webbrowser.open(out.resolve().as_uri())


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
