"""The ``witan-code`` CLI (code graph + cross-repo bridge).

Exposed standalone as ``witan-code`` and mounted as ``witan code`` (see
pyproject ``[project.scripts]``).
"""

from pathlib import Path
from typing import Annotated, Literal

import cyclopts
from witan_core.cli import (
    AGENT_NAMES,
    AgentName,
    make_app,
    report_install,
    resolve_author,
)

from . import indexer
from .output import OutputFormat, dump_structured, get_output_format, set_output_format

app = make_app(
    name="witan-code",
    help_text="witan-code — tree-sitter code graph + cross-repo bridge.",
    version_dist="witan-code",
)

BindingKind = Literal["env_var", "package", "service", "endpoint"]


def _render_table(
    *,
    title: str,
    columns: list[str],
    rows: list[dict[str, object]],
    no_wrap: set[str] | None = None,
) -> None:
    """Render ``rows`` as a rich table, or dump them per ``--output-format``."""
    rows = [{k: ("" if v is None else v) for k, v in r.items()} for r in rows]

    fmt = get_output_format()
    if fmt != "txt":
        dump_structured(rows, title, fmt)
        return

    from rich.console import Console
    from rich.table import Table

    table = Table(title=title, header_style="bold")
    no_wrap = no_wrap or set()
    for col in columns:
        if col in no_wrap:
            table.add_column(col, no_wrap=True)
        else:
            table.add_column(col, overflow="fold", no_wrap=False)
    for row in rows:
        table.add_row(*(str(row.get(col, "")) for col in columns))
    Console().print(table)


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

    table_rows: list[dict[str, object]] = []
    for r in rows:
        conf = r.get("confidence")
        where = (
            f"{r.get('file') or ''}:{r.get('line')}"
            if r.get("line")
            else (r.get("file") or "")
        )
        table_rows.append(
            {
                "role": r.get("role", ""),
                "symbol": r.get("symbol", ""),
                "kind": r.get("kind", ""),
                "refs": r.get("n_refs", ""),
                "conf": round(float(conf), 2) if isinstance(conf, (int, float)) else "",
                "where": where,
            }
        )
    _render_table(
        title=f"Symbol table — {repo}",
        columns=["role", "symbol", "kind", "refs", "conf", "where"],
        rows=table_rows,
    )


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
        table_rows: list[dict[str, object]] = []
        for r in sorted(
            unresolved_rows, key=lambda r: (r["repo"] or "", r["symbol"] or "")
        ):
            table_rows.append(
                {
                    "repo": r["repo"] or "",
                    "symbol": r["symbol"] or "",
                    "kind": r["kind"] or "",
                    "refs": r.get("n_refs"),
                }
            )
        _render_table(
            title="Unresolved external symbols",
            columns=["repo", "symbol", "kind", "refs"],
            rows=table_rows,
        )
        return

    if repo is not None:
        edges = [e for e in edges if repo in (e.consumer_repo, e.provider_repo)]
    if not edges:
        console.print("[dim]No precise cross-repo edges.[/dim]")
        return
    table_rows = [
        {
            "consumer": e.consumer_repo or "",
            "provider": e.provider_repo or "",
            "kind": e.kind or "",
            "matches": e.match_count,
            "preferred": "yes" if e.preferred else "",
            "ambiguous": "yes" if e.ambiguous_version else "",
        }
        for e in sorted(
            edges,
            key=lambda e: (e.consumer_repo or "", e.provider_repo or "", e.kind or ""),
        )
    ]
    _render_table(
        title="Precise cross-repo edges (Stage 2)",
        columns=["consumer", "provider", "kind", "matches", "preferred", "ambiguous"],
        rows=table_rows,
    )


@app.command(name="inject-context")
def inject_context_cmd() -> None:
    """Print a short code-graph status block for the UserPromptSubmit hook.

    Registered as the bare ``UserPromptSubmit`` hook command; always exits 0
    and prints nothing when there's no store or in-flight index for the
    current repo.
    """
    import sys

    from . import context as context_module

    try:
        text = context_module.inject_context()
    except Exception:  # noqa: BLE001 — must never fail the hook
        return
    if text:
        sys.stdout.write(text)  # inject_context() already ends with "\n"


@app.command
def serve() -> None:
    """Run the code-graph MCP server standalone (code_* tools only)."""
    from .server import mcp

    mcp.run()


def _resolve_store(store: str | None, *, bridge: bool = False) -> Path | None:
    """Resolve a store path — explicit, the shared bridge, or the current
    repo's — printing and returning ``None`` if it doesn't exist yet (nothing
    to compact). Expands ``~`` in an explicit path so ``--store ~/…`` isn't
    treated as missing.
    """
    from . import config as cfg_module
    from . import repo as repo_module

    cfg = cfg_module.load()
    if store is not None:
        path = Path(store).expanduser()
    elif bridge:
        path = cfg_module.bridge_store_path(cfg.code_dir)
    else:
        slug = repo_module.detect()
        if slug is None:
            print("No repo detected — pass --store PATH or --bridge.")
            return None
        path = cfg_module.store_path(slug, cfg.code_dir)
    if not path.exists():
        print(f"No store at {path} — nothing to do.")
        return None
    return path


def _maintenance_client(store: Path):
    from . import config as cfg_module
    from .graph import OmnigraphClient

    return OmnigraphClient(str(store), cfg_module.load().queries_dir)


@app.command
def optimize(*, store: str | None = None, bridge: bool = False) -> None:
    """Compact a code-graph store's Lance fragments (non-destructive).

    Collapses the many tiny fragments that accrue from every index/reindex so
    opening the store stays cheap. Safe to run repeatedly; takes the store's
    write lock.

    Parameters
    ----------
    store: Store path to optimize (default: the current repo's store).
    bridge: Optimize the shared cross-repo bridge store instead.
    """
    path = _resolve_store(store, bridge=bridge)
    if path is None:
        return
    print(f"Optimizing {path} …")
    _maintenance_client(path).optimize()
    print("Optimized. (run `witan-code cleanup` to reclaim disk)")


@app.command
def cleanup(
    *,
    store: str | None = None,
    bridge: bool = False,
    keep: int = 10,
    older_than: str | None = None,
    yes: bool = False,
) -> None:
    """Remove old Lance versions from a code-graph store (**destructive**).

    ``optimize`` compacts fragments but leaves old versions behind; this GCs
    them, keeping the most recent ``keep`` versions per table (and/or those
    newer than ``older_than``). Irreversible, so it requires ``--yes``.

    Parameters
    ----------
    store: Store path to clean (default: the current repo's store).
    bridge: Clean the shared cross-repo bridge store instead.
    keep: Number of recent versions to keep per table.
    older_than: Also keep versions newer than this Go-style duration (e.g. 7d).
    yes: Confirm the destructive operation (required to actually run).
    """
    path = _resolve_store(store, bridge=bridge)
    if path is None:
        return
    if not yes:
        print(
            f"cleanup is destructive — would keep the {keep} most recent "
            f"version(s) per table"
            + (f" and anything newer than {older_than}" if older_than else "")
            + f" in {path}.\nRe-run with --yes to proceed."
        )
        return
    print(f"Cleaning up {path} (keep={keep}) …")
    _maintenance_client(path).cleanup(keep=keep, older_than=older_than)
    print("Cleaned up.")


@app.command
def checkpoint() -> None:
    """Opportunistically compact the current repo's store(s) (Stop hook).

    Spawns a throttled, detached ``witan-code optimize`` for the current
    repo's store and the shared bridge store, each at most once per
    ``WITAN_CODE_OPTIMIZE_INTERVAL``, if either exists and is due. Best-effort
    and non-blocking: always exits 0 and never raises, so a maintenance
    failure can't fail the Stop hook. Registered as the bare ``Stop`` hook
    command; not usually run by hand.
    """
    from . import config as cfg_module
    from . import maintenance as maintenance_module
    from . import repo as repo_module

    cfg = cfg_module.load()
    slug = repo_module.detect()
    if slug is not None:
        try:
            maintenance_module.spawn_background_optimize(
                cfg_module.store_path(slug, cfg.code_dir)
            )
        except Exception:  # noqa: BLE001 — maintenance must never fail the hook
            pass
    try:
        maintenance_module.spawn_background_optimize(
            cfg_module.bridge_store_path(cfg.code_dir)
        )
    except Exception:  # noqa: BLE001 — maintenance must never fail the hook
        pass


@app.command(name="session-init")
def session_init_cmd() -> None:
    """Seed/refresh the whole repo's code graph in the background (SessionStart hook).

    Detached and non-blocking — returns immediately regardless of repo size.
    A per-repo lock (shared with ``inject-context``'s "indexing in progress"
    check) prevents overlapping sessions from indexing at once. Registered as
    the bare ``SessionStart`` hook command; not usually run by hand.
    """
    from . import hooks as hooks_module

    try:
        hooks_module.session_init()
    except Exception:  # noqa: BLE001 — must never fail the hook
        pass


@app.command(name="_index-and-unlock", show=False)
def _index_and_unlock_cmd(target: Path, lock: Path) -> None:
    """Internal — run only by the detached child ``session-init`` spawns."""
    from . import hooks as hooks_module

    hooks_module.index_and_unlock(target, lock)


@app.command(name="reindex-hook")
def reindex_hook_cmd() -> None:
    """Incrementally reindex the file named in stdin's hook JSON (PostToolUse hook).

    Reads the Claude Code hook payload from stdin, extracts
    ``tool_input.file_path`` (or ``path``/``filename``), and reindexes it if
    it exists and is a known source type — foreground and fast (one file), so
    the agent sees the change land immediately. Best-effort: a missing or
    malformed payload is a silent no-op. Registered as the bare
    ``PostToolUse`` (matcher ``Edit|Write``) hook command; not usually run by
    hand.
    """
    import sys

    from . import hooks as hooks_module

    try:
        hooks_module.reindex_hook(sys.stdin.read())
    except Exception:  # noqa: BLE001 — must never fail the hook
        pass


@app.command
def setup(
    *,
    agent: AgentName = "claude",
    author: str | None = None,
    dry_run: bool = False,
) -> None:
    """Install witan-code for one or all supported coding agents.

    Installs the omnigraph binary to ~/.local/bin/, copies the bundled skill
    and Pi extension to the agent's config directories, registers the four
    hooks (bare CLI commands — no wrapper scripts to copy), and merges the
    witan-code MCP server entry into the agent's config file. Independent of
    `witan setup` — running both is fine (each only touches its own entries);
    running just this one is enough for a witan-code-only install.

    Re-run after every upgrade to refresh installed files.

    Parameters
    ----------
    agent: Target agent — claude | pi | copilot | opencode | all.
    author: Name written to graph nodes (default: git config user.name or $USER).
    dry_run: Print what would happen without writing anything.
    """
    from agent_config_kit import (
        apply,
        apply_all,
        detect_installed_platforms,
        known_platforms,
    )

    from witan_core import install_omnigraph

    from .setup import witan_code_bundle

    pkg_dir = Path(__file__).parent

    author = resolve_author(author)

    print("omnigraph binary")
    install_omnigraph(dry_run)

    bundle = witan_code_bundle(pkg_dir, author)

    if agent == "all":
        for name, result in apply_all(bundle, dry_run=dry_run).items():
            report_install(name, result, dry_run=dry_run)
        for name in sorted(set(known_platforms()) - set(detect_installed_platforms())):
            print(f"\n{AGENT_NAMES.get(name, name)} — not detected, skipping")
    else:
        report_install(agent, apply(agent, bundle, dry_run=dry_run), dry_run=dry_run)

    if dry_run:
        print("\n(dry-run — no files written)")
    else:
        print("\nDone. Restart your agent(s) to pick up the new MCP server and hooks.")


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

    table_rows: list[dict[str, object]] = []
    for store in stores:
        repo_uri, file_count = _code_store_stats(store)
        size, mtime = _dir_stats(store)
        table_rows.append(
            {
                "repo": repo_uri,
                "files": file_count,
                "size": _human_size(size),
                "last indexed": mtime,
            }
        )
    _render_table(
        title="Indexed repositories",
        columns=["repo", "files", "size", "last indexed"],
        rows=table_rows,
        no_wrap={"files", "size", "last indexed"},
    )


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


@app.meta.default
def _launcher(
    *tokens: Annotated[str, cyclopts.Parameter(show=False, allow_leading_hyphen=True)],
    output_format: Annotated[
        OutputFormat,
        cyclopts.Parameter(name="--output-format", env_var="WITAN_OUTPUT_FORMAT"),
    ] = "txt",
) -> None:
    """witan-code — tree-sitter code graph + cross-repo bridge.

    Parameters
    ----------
    output_format: Output format for table commands. Commands: repos, symbols,
        stitch. Values: txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT.
    """
    set_output_format(output_format)
    app(tokens)


def cli() -> None:
    app.meta()


if __name__ == "__main__":
    cli()
