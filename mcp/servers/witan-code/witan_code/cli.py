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
    min_precision: Literal["precise", "heuristic", "fuzzy"] = "heuristic",
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
    min_precision:
        Minimum edge precision tier (docs/EDGE_PRECISION_TIERS.md). Default
        `heuristic` preserves prior behavior (every consumer/provider link
        this command has always shown). `precise` keeps only edges also
        covered by a Stage-2 canonical-symbol join — see `witan code stitch`.
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
    repo_symbol_rows = (
        client.read("bridge.gq", "all_repo_symbols", {})
        if min_precision == "precise"
        else None
    )
    graph = visualize.build_graph(
        rows,
        kind=kind,
        repo=repo,
        min_precision=min_precision,
        repo_symbol_rows=repo_symbol_rows,
    )
    visualize.render_rich(graph)

    if html is not None:
        out = visualize.render_html(graph, html)
        print(f"\nwrote {out}")
        if open_browser:
            import webbrowser

            webbrowser.open(out.resolve().as_uri())


@app.command
def symbols(
    repo: str | None = None,
    role: Literal["exported", "external"] | None = None,
    scheme: str | None = None,
) -> None:
    """Print a repo's symbol table from the bridge store (docs/SYMBOL_TABLE.md).

    One row per (role, symbol): `exported` rows are the repo's public contract
    surface; `external` rows are unresolved references Stage 2 joins against
    other repos' exports.

    Parameters
    ----------
    repo:
        Canonical repo URI. Defaults to the repo detected from the CWD.
    role:
        Filter to exported or external rows.
    scheme:
        Filter to one symbol scheme (http/env/pkg/svc).
    """
    from rich.console import Console
    from rich.table import Table

    from . import config as cfg_module
    from . import repo as repo_module
    from . import store as store_module
    from .graph import OmnigraphClient

    console = Console()
    cfg = cfg_module.load()
    store = store_module.bridge_store(cfg)
    if not store.exists():
        console.print(
            "No bridge store yet — run `witan code index` in your repos first."
        )
        return

    repo = repo or repo_module.detect()
    if not repo:
        console.print("No repo detected — pass --repo <canonical URI>.")
        return

    client = OmnigraphClient(str(store), cfg.queries_dir)
    rows = client.read("bridge.gq", "repo_symbols", {"repo": repo})
    rows = [
        r
        for r in rows
        if (role is None or r.get("role") == role)
        and (scheme is None or r.get("scheme") == scheme)
    ]
    if not rows:
        console.print(f"[dim]No symbol table rows for {repo}.[/dim]")
        return

    table = Table(title=f"Symbol table — {repo}", header_style="bold")
    for col in ("role", "symbol", "kind", "refs", "conf", "where"):
        table.add_column(col)
    for r in rows:
        conf = r.get("confidence")
        where = (
            f"{r.get('file') or ''}:{r.get('line')}"
            if r.get("line")
            else (r.get("file") or "")
        )
        table.add_row(
            r.get("role", ""),
            r.get("symbol", ""),
            r.get("kind", ""),
            str(r.get("n_refs", "")),
            f"{conf:.2f}" if isinstance(conf, (int, float)) else "",
            where,
        )
    console.print(table)


@app.command
def stitch(repo: str | None = None, *, unresolved: bool = False) -> None:
    """Print Stage-2 precise cross-repo edges from the bridge store (docs/SYMBOL_TABLE.md).

    Joins every repo's unresolved external symbols against other repos'
    exported symbols by canonical symbol string — distinct from the coarser
    `witan code deps` heuristic (kind, key_norm) grouping.

    Parameters
    ----------
    repo:
        Keep only edges/gaps touching this repo. Omit to see the whole store.
    unresolved:
        Print external references with no precise match instead of edges —
        gaps in indexing coverage (a provider isn't indexed yet, or none
        exists in this SOA).
    """
    from rich.console import Console
    from rich.table import Table

    from . import config as cfg_module
    from . import store as store_module
    from . import stitch as stitch_module
    from .graph import OmnigraphClient

    console = Console()
    cfg = cfg_module.load()
    store = store_module.bridge_store(cfg)
    if not store.exists():
        console.print(
            "No bridge store yet — run `witan code index` in your repos first."
        )
        return

    client = OmnigraphClient(str(store), cfg.queries_dir)
    rows = client.read("bridge.gq", "all_repo_symbols", {})
    edges, unresolved_rows = stitch_module.resolve(rows)

    if unresolved:
        if repo is not None:
            unresolved_rows = [r for r in unresolved_rows if r["repo"] == repo]
        if not unresolved_rows:
            console.print("[dim]No unresolved external symbols.[/dim]")
            return
        table = Table(title="Unresolved external symbols", header_style="bold")
        for col in ("repo", "symbol", "kind", "refs"):
            table.add_column(col)
        for r in sorted(
            unresolved_rows, key=lambda r: (r["repo"] or "", r["symbol"] or "")
        ):
            n_refs = r.get("n_refs")
            table.add_row(
                r["repo"] or "",
                r["symbol"] or "",
                r["kind"] or "",
                str(n_refs) if n_refs is not None else "",
            )
        console.print(table)
        return

    if repo is not None:
        edges = [e for e in edges if repo in (e.consumer_repo, e.provider_repo)]
    if not edges:
        console.print("[dim]No precise cross-repo edges.[/dim]")
        return
    table = Table(title="Precise cross-repo edges (Stage 2)", header_style="bold")
    for col in ("consumer", "provider", "kind", "matches", "preferred", "ambiguous"):
        table.add_column(col)
    for e in sorted(
        edges,
        key=lambda e: (e.consumer_repo or "", e.provider_repo or "", e.kind or ""),
    ):
        table.add_row(
            e.consumer_repo or "",
            e.provider_repo or "",
            e.kind or "",
            str(e.match_count),
            "yes" if e.preferred else "",
            "yes" if e.ambiguous_version else "",
        )
    console.print(table)


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


@app.command
def branches(*, prune: bool = False) -> None:
    """List omnigraph branches per indexed repo store.

    Non-default git branches index onto same-named omnigraph branches
    (docs/BRANCH_INDEXING.md). Branch stores are re-derivable caches, so
    lifecycle is deletion, not merge.

    Parameters
    ----------
    prune:
        Delete the CURRENT repo's store branches whose git branch no longer
        exists locally, plus the ``_detached`` scratch branch. Other repos'
        stores are only listed (their git refs aren't visible from here).
    """
    from . import config as cfg_module
    from . import repo as repo_module
    from .graph import OmnigraphClient

    cfg = cfg_module.load()
    if not cfg.code_dir.is_dir():
        print(f"No code stores at {cfg.code_dir}.")
        return

    current_slug = repo_module.detect()
    current_store = (
        cfg_module.store_path(current_slug, cfg.code_dir) if current_slug else None
    )
    git_branches = repo_module.local_branches() if prune else None

    stores = [
        p
        for p in sorted(cfg.code_dir.glob("*.omni"))
        if p.name != cfg_module.BRIDGE_STORE_NAME
    ]
    for store in stores:
        client = OmnigraphClient(str(store), cfg.queries_dir)
        try:
            names = client.list_branches()
        except Exception as exc:  # noqa: BLE001 — one bad store shouldn't abort
            print(f"{_store_repo(store)}: <error: {exc}>")
            continue
        extra = [n for n in names if n != "main"]
        print(f"{_store_repo(store)}: main" + ("," + ",".join(extra) if extra else ""))

        if not (
            prune
            and current_store is not None
            and store == current_store
            and git_branches is not None
        ):
            continue
        for name in extra:
            if name == repo_module.DETACHED_BRANCH or name not in git_branches:
                client.delete_branch(name)
                print(f"  pruned {name}")


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
