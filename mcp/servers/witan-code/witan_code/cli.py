"""The ``witan-code`` CLI (code graph + cross-repo bridge).

Exposed standalone as ``witan-code`` and mounted as ``witan code`` (see
pyproject ``[project.scripts]``).
"""

from pathlib import Path
from typing import Literal

import cyclopts

from . import indexer

app = cyclopts.App(
    name="witan-code",
    help="witan-code — tree-sitter code graph + cross-repo bridge.",
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
        print("No bridge store yet — run `witan code index` in your repos first.")
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


@app.command
def serve() -> None:
    """Run the code-graph MCP server standalone (code_* tools only)."""
    from .server import mcp

    mcp.run()


@app.command
def setup(*, dry_run: bool = False) -> None:
    """Install the omnigraph binary to ~/.local/bin/ for standalone witan-code use.

    Only needed when running witan-code without witan already installed —
    `witan setup` installs the same binary to the same place. Re-run after an
    omnigraph version bump to refresh it.
    """
    from .setup import install_omnigraph

    install_omnigraph(dry_run=dry_run)


def _print_summary(action: str, path: Path, stats: indexer.IndexStats) -> None:
    print(
        f"{action} {path}: "
        f"scanned={stats.scanned} indexed={stats.indexed} "
        f"skipped={stats.skipped} symbols={stats.symbols} "
        f"edges={stats.edges} bindings={stats.bindings} errors={stats.errors}"
    )


# ── Indexed repositories ─────────────────────────────────────────────────────


@app.command
def repos() -> None:
    """List the repositories that have a code graph indexed."""
    from rich.console import Console
    from rich.table import Table

    from . import config as cfg_module

    console = Console()
    code_dir = cfg_module.load().code_dir
    if not code_dir.is_dir():
        console.print(f"[dim]No code stores at {code_dir}.[/dim]")
        return
    # Exclude the shared cross-repo bridge store — it isn't a repo.
    stores = [
        p
        for p in sorted(code_dir.glob("*.omni"))
        if p.name != cfg_module.BRIDGE_STORE_NAME
    ]
    if not stores:
        console.print(f"[dim]No indexed repositories in {code_dir}.[/dim]")
        return

    table = Table(title="Indexed repositories", header_style="bold")
    for col in ("repo", "files", "size", "last indexed"):
        table.add_column(col)
    for store in stores:
        repo_uri, file_count = _code_store_stats(store)
        size, mtime = _dir_stats(store)
        table.add_row(repo_uri, file_count, _human_size(size), mtime)
    console.print(table)


def _code_store_stats(store: Path) -> tuple[str, str]:
    """Return (repo_uri, file_count); repo URI comes from the sidecar."""
    repo_uri = _store_repo(store)
    try:
        from . import config as cfg_module
        from .graph import OmnigraphClient

        client = OmnigraphClient(str(store), cfg_module.load().queries_dir)
        rows = client.read("code_read.gq", "all_file_hashes", {})
        return repo_uri, str(len(rows))
    except Exception:  # noqa: BLE001 — degrade gracefully
        return repo_uri, "?"


def _store_repo(store: Path) -> str:
    """Canonical repo URI for a store: the exact sidecar if present, else a
    best-effort reconstruction from the (lossily) sanitized filename."""
    from . import store as store_module

    sidecar = store_module.repo_sidecar(store)
    if sidecar.exists():
        return sidecar.read_text().strip()
    return _repo_from_stem(store.stem)


def _repo_from_stem(stem: str) -> str:
    """Best-effort canonical repo URI from a sanitized store filename.

    The store name is ``sanitize_slug(repo)`` (``[/:]+`` collapsed to ``_``), so
    a 0-file store has no CodeFile to read the exact repo from. For the common
    ``scheme://host/path`` slug, reconstruct it: ``https_github.com_org_repo`` →
    ``https://github.com/org/repo``. A schemeless local slug is returned as-is.
    """
    for scheme in ("https", "http", "ssh"):
        prefix = f"{scheme}_"
        if stem.startswith(prefix):
            return f"{scheme}://{stem[len(prefix) :].replace('_', '/')}"
    return stem


def _dir_stats(path: Path) -> tuple[int, str]:
    """Return (total_bytes, last-modified string) in a single directory walk."""
    import datetime

    total = 0
    latest = path.stat().st_mtime
    for f in path.rglob("*"):
        if f.is_file():
            st = f.stat()
            total += st.st_size
            if st.st_mtime > latest:
                latest = st.st_mtime
    return total, datetime.datetime.fromtimestamp(latest).strftime("%Y-%m-%d %H:%M")


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def cli() -> None:
    app()


if __name__ == "__main__":
    cli()
