"""The ``witan-code`` CLI (code graph + cross-repo bridge).

Exposed standalone as ``witan-code`` and mounted as ``witan code`` (see
pyproject ``[project.scripts]``).

Read commands (``symbols``, ``deps``, ``stitch``, ``repos``, ``branches``)
dispatch through :func:`_srv`, which is the ``witan_code.server`` module
in-process by default and a ``RemoteServerProxy`` when a deployed endpoint is
configured (ADR 0005, path a) — so the same command reads the local stores or a
deployment's without any call site knowing which. Write and maintenance
commands (``index``, ``reindex``, ``optimize``, ``cleanup``, ``checkpoint``,
the hooks) need the checkout and the store files on disk, so they stay local
unconditionally.
"""

import asyncio
import inspect
import sys
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

_server = None


def _srv():
    """Return the tool provider the read commands dispatch through.

    In-process ``witan_code.server`` by default; a network-dispatching
    ``RemoteServerProxy`` when ``WITAN_REMOTE_URL`` (or a matched target's
    ``remote_url``) is set. The proxy mirrors the server module's tool surface,
    so every call site is identical either way.
    """
    global _server
    if _server is None:
        from . import config as cfg_module

        # A misconfigured remote (e.g. WITAN_REMOTE_URL without
        # WITAN_OIDC_ISSUER) raises ValueError here; surface it as a clean CLI
        # error rather than letting a traceback escape every read command.
        try:
            remote = cfg_module.load_remote_config()
        except ValueError as exc:
            print(exc)
            raise SystemExit(1) from None
        if remote is not None:
            from .remote.oidc import default_token_provider, default_token_refresher
            from .remote.proxy import RemoteServerProxy

            _server = RemoteServerProxy(
                remote,
                default_token_provider(remote),
                default_token_refresher(remote),
            )
        else:
            from . import server as server_module

            _server = server_module
    return _server


def _fn(tool):
    """Unwrap a FastMCP-decorated tool to a directly-callable function.

    Tools that gained MCP elicitation are ``async def`` (they take a
    ``ctx: Context`` FastMCP injects). The CLI calls them directly, not through
    an MCP client, so wrap a coroutine tool to run to completion via
    ``asyncio.run`` — with no ctx it falls back to its non-interactive default,
    which is the right behavior for a plain ``witan-code …`` command. The
    remote proxy's attributes are already plain callables, so this is a no-op
    against it.
    """
    fn = getattr(tool, "fn", tool)
    if inspect.iscoroutinefunction(fn):

        def runner(*args, **kwargs):
            return asyncio.run(fn(*args, **kwargs))

        return runner
    return fn


def _is_remote() -> bool:
    """Whether reads are currently dispatching to a deployment."""
    from . import server as server_module

    return _srv() is not server_module


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
    _print_summary("index", path, _index(path, force=False))


@app.command
def reindex(path: Path = Path(".")) -> None:
    """Force re-index PATH, ignoring content hashes."""
    _print_summary("reindex", path, _index(path, force=True))


def _index(path: Path, *, force: bool) -> indexer.IndexStats:
    """``index_path``, but a failure still prints what the run got through.

    The parse phase's counts are the same numbers a success prints, and they are
    what says whether a write failure is a big-repo problem or not — so losing
    them to the traceback is losing the diagnosis. Printed here rather than
    swallowed: the exception still propagates, so the exit code and the
    traceback are unchanged.
    """
    try:
        return indexer.index_path(path, force=force)
    except indexer.IndexFailed as exc:
        _print_summary("partial", path, exc.stats)
        print(f"failed in {exc.phase}: {exc}", file=sys.stderr)
        raise


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
    from . import visualize

    payload = _fn(_srv().code_repo_dependencies)(
        kind=kind, repo=repo, min_precision=min_precision
    )
    graph = visualize.from_payload(payload)
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

    from . import repo as repo_module

    console = Console()
    # Resolved here, not server-side: a deployment has no checkout to detect
    # from, and the table title names the repo either way.
    repo = repo or repo_module.detect()
    if not repo:
        console.print("No repo detected — pass --repo <canonical URI>.")
        return

    rows = _fn(_srv().code_repo_symbols)(repo=repo, role=role, scheme=scheme)
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

    console = Console()

    if unresolved:
        unresolved_rows = _fn(_srv().code_unresolved_symbols)(repo=repo)
        if not unresolved_rows:
            console.print("[dim]No unresolved external symbols.[/dim]")
            return
        table_rows: list[dict[str, object]] = [
            {
                "repo": r["repo"] or "",
                "symbol": r["symbol"] or "",
                "kind": r["kind"] or "",
                "refs": r.get("n_refs"),
            }
            for r in sorted(
                unresolved_rows, key=lambda r: (r["repo"] or "", r["symbol"] or "")
            )
        ]
        _render_table(
            title="Unresolved external symbols",
            columns=["repo", "symbol", "kind", "refs"],
            rows=table_rows,
        )
        return

    edges = _fn(_srv().code_precise_edges)(repo=repo)
    if not edges:
        console.print("[dim]No precise cross-repo edges.[/dim]")
        return
    table_rows = [
        {
            "consumer": e["consumer_repo"] or "",
            "provider": e["provider_repo"] or "",
            "kind": e["kind"] or "",
            "matches": e["match_count"],
            "preferred": "yes" if e["preferred"] else "",
            "ambiguous": "yes" if e["ambiguous_version"] else "",
        }
        for e in sorted(
            edges,
            key=lambda e: (
                e["consumer_repo"] or "",
                e["provider_repo"] or "",
                e["kind"] or "",
            ),
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
    """Run the code-graph MCP server standalone (code_* tools only).

    When witan-code is mounted into the umbrella ``witan serve`` instead, that
    command has already configured observability; the call here is idempotent so
    the standalone path gets it too without double-configuring the combined one.
    """
    from witan_core.observability import configure_observability

    configure_observability()

    from .server import mcp

    mcp.run()


def _resolve_store(store: str | None, *, bridge: bool = False):
    """Resolve a store to compact — explicit, the shared bridge, or the current
    repo's — printing and returning ``None`` if there is nothing to compact.
    Expands ``~`` in an explicit path so ``--store ~/…`` isn't treated as
    missing.

    A cluster graph is refused rather than resolved: ``optimize``/``cleanup``
    are direct-storage commands (they reject ``--server``) and compacting the
    shared store is the cluster's own job, run against the S3 root by a
    maintenance CronJob, not by whichever client happened to finish a session.

    An explicit ``--store`` is only run through ``Path`` when it is NOT a URL:
    ``Path("https://host").expanduser()`` collapses the ``//`` to ``/``, which
    left ``https:/host`` failing the http(s) test and being refused as a
    missing local directory — the one input the cluster refusal above exists
    to catch.
    """
    from . import config as cfg_module
    from . import repo as repo_module
    from . import store as store_module

    cfg = cfg_module.load()
    if store is not None:
        raw = (
            store
            if store.startswith(("http://", "https://"))
            else str(Path(store).expanduser())
        )
        ref = store_module.StoreRef(raw)
    elif bridge:
        ref = store_module.bridge_store(cfg)
    else:
        slug = repo_module.detect()
        if slug is None:
            print("No repo detected — pass --store PATH or --bridge.")
            return None
        ref = store_module.store_for_repo(slug, cfg)
    if ref.is_remote:
        print(
            f"{ref} is a shared cluster graph — compaction runs server-side "
            "against the storage root, not from a client. Nothing to do here."
        )
        return None
    if not ref.exists():
        print(f"No store at {ref} — nothing to do.")
        return None
    return ref


def _maintenance_client(ref):
    from . import config as cfg_module

    return ref.client(cfg_module.load())


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
    ref = _resolve_store(store, bridge=bridge)
    if ref is None:
        return
    print(f"Optimizing {ref} …")
    _maintenance_client(ref).optimize()
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
    ref = _resolve_store(store, bridge=bridge)
    if ref is None:
        return
    if not yes:
        print(
            f"cleanup is destructive — would keep the {keep} most recent "
            f"version(s) per table"
            + (f" and anything newer than {older_than}" if older_than else "")
            + f" in {ref}.\nRe-run with --yes to proceed."
        )
        return
    print(f"Cleaning up {ref} (keep={keep}) …")
    _maintenance_client(ref).cleanup(keep=keep, older_than=older_than)
    print("Cleaned up.")


@app.command(name="reap-views")
def reap_views(
    *,
    store: str | None = None,
    graph: str | None = None,
    max_idle_days: float | None = None,
    apply: bool = False,
) -> None:
    """Delete branch views nobody has written in a long time (**destructive**).

    On a shared cluster graph every developer's every git branch gets a view of
    its own, and nothing ever removes one — this is what bounds that. Views are
    re-derivable caches, so a reaped view costs its owner a reindex, not work.

    Distinct from ``branches --prune``, which asks whether *this checkout* still
    has the git branch and so only makes sense against a store this machine
    alone writes. This asks how long ago a view was last written, which a shared
    graph can answer for every writer. A view with no writes of its own is never
    reaped: it holds nothing, and there is no creation timestamp to age it by.

    Reports by default; ``--apply`` is what deletes. On a shared graph deleting
    requires ``WITAN_CODE_INDEX_ROLE=ci`` — Cedar grants ``branch_delete`` to
    the CI indexer alone, and refusing here makes that a clear local error
    rather than a server denial.

    Parameters
    ----------
    store: Graph to sweep — a store path, or an ``http(s)://`` omnigraph-server
        URL. Default: every store this config resolves to (cluster graphs when
        ``code_server`` is set, else the local ones), the shared bridge
        included.
    graph: Cluster graph id, when ``--store`` is a server URL that doesn't
        encode one as ``.../graphs/<id>``.
    max_idle_days: Reap views idle at least this long (default: 14, or
        ``WITAN_CODE_VIEW_MAX_IDLE_DAYS``). ``0`` disables reaping.
    apply: Actually delete. Without it, nothing is written.
    """
    from . import config as cfg_module
    from . import reaper as reaper_module
    from . import store as store_module
    from .graph import OmnigraphClient

    cfg = cfg_module.load()
    if store is None and cfg.code_transport == cfg_module.CODE_TRANSPORT_MCP:
        # Same shape as optimize/cleanup's refusal: reaping reads each view's
        # commit log and deletes branches, neither of which the MCP tier serves
        # — it is the cluster's own scheduled job, run with the CI role.
        print(
            "Code graphs are reached through the deployed witan endpoint "
            f"({cfg.target_name or 'code_transport = mcp'}), which does not "
            "serve view reaping — it runs in-cluster, as the CI indexer. "
            "Nothing to do here."
        )
        return
    try:
        idle = reaper_module.max_idle_days() if max_idle_days is None else max_idle_days
    except ValueError as exc:
        print(exc)
        raise SystemExit(1) from None

    if store is not None:
        targets = [(store, OmnigraphClient(store, cfg.queries_dir, graph_id=graph))]
    else:
        refs = [
            *store_module.per_repo_stores(cfg),
            store_module.bridge_store(cfg),
        ]
        targets = [(str(r), r.client(cfg)) for r in refs if r.exists(cfg)]
    if not targets:
        print("No code-graph stores — nothing to reap.")
        return
    if idle <= 0:
        print(
            f"Reaping is disabled ({reaper_module.MAX_IDLE_ENV_VAR}=0) — "
            f"{len(targets)} store(s) left untouched."
        )
        return

    failed_graphs = 0
    for name, client in targets:
        try:
            report = reaper_module.reap(
                client, graph=name, max_idle=idle, apply=apply, cfg=cfg
            )
        except PermissionError as exc:
            print(exc)
            raise SystemExit(1) from None
        except RuntimeError as exc:
            # A graph that can't be surveyed (unreadable store, unexpected
            # omnigraph output) must not strand the others — but it must not
            # pass for a clean sweep either, so it shows up in the exit code.
            print(f"{name}: FAILED to survey — {exc}")
            failed_graphs += 1
            continue
        _print_reap_report(report, idle=idle)

    if not apply:
        print("\n(report only — re-run with --apply to delete)")
    if failed_graphs:
        raise SystemExit(1)


def _print_reap_report(report, *, idle: float) -> None:
    print(
        f"{report.graph}: {report.scanned} view(s), {len(report.stale)} idle ≥{idle:g}d"
    )
    deleted = set(report.deleted)
    for age in report.stale:
        verb = "reaped" if age.view in deleted else "stale "
        days = age.idle_days(report.now)
        print(f"  {verb} {age.view} (owner {age.owner or 'nobody'}, idle {days:.1f}d)")
    for view, err in report.failed:
        print(f"  FAILED {view}: {err}")


@app.command
def checkpoint() -> None:
    """Opportunistically compact the current repo's store(s) (Stop hook).

    Spawns a throttled, detached ``witan-code optimize`` for the current
    repo's store and the shared bridge store, each at most once per
    ``WITAN_CODE_OPTIMIZE_INTERVAL``, if either exists and is due. Best-effort
    and non-blocking: always exits 0 and never raises, so a maintenance
    failure can't fail the Stop hook. Registered as the bare ``Stop`` hook
    command; not usually run by hand.

    A no-op against cluster graphs — ``maintenance.due()`` never fires for a
    remote store, since compacting the shared storage root is the cluster's
    job rather than every client's at the end of every session.
    """
    from . import config as cfg_module
    from . import maintenance as maintenance_module
    from . import repo as repo_module
    from . import store as store_module

    cfg = cfg_module.load()
    slug = repo_module.detect()
    refs = [store_module.bridge_store(cfg)]
    if slug is not None:
        refs.insert(0, store_module.store_for_repo(slug, cfg))
    for ref in refs:
        try:
            maintenance_module.spawn_background_optimize(ref.uri)
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
    # strict=False, same reasoning as `witan setup`: a refused binary must not
    # cost the user the agent bundles this command also installs. The installer
    # prints the refusal regardless.
    install_omnigraph(dry_run, strict=False)

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
        # Only when it happened: a purge is newsworthy (rows were deleted),
        # but printing purged=0 on every routine index is noise.
        + (f" purged={stats.purged}" if stats.purged else "")
        # Same rule, opposite reason: `bindings=0` is unremarkable on a run
        # with nothing to write and alarming on one whose bridge write threw,
        # and the number alone cannot tell you which. Say so in the one line
        # anybody reads.
        + (" bridge=FAILED" if stats.bridge_failed else "")
    )


@app.command
def branches(*, branch: str | None = None, prune: bool = False) -> None:
    """List the in-flight branch views per indexed repo store, and who owns each.

    A non-default git branch is indexed onto its own view, named for its
    writer as well as the branch (docs/BRANCH_INDEXING.md), so two checkouts
    of the same branch do not overwrite each other. Views are re-derivable
    caches, so lifecycle is deletion, not merge.

    Parameters
    ----------
    branch:
        Show only views of this git branch — every writer's, which is how you
        find a teammate's in-flight work. Pass a listed view name to
        ``--branch`` on the read commands to query it.
    prune:
        Delete the CURRENT repo's views whose git branch no longer exists
        locally, plus the ``_detached`` scratch view. Other repos' stores are
        only listed (their git refs aren't visible from here). Local stores
        only, on both counts below: pruning reads this machine's git refs as
        the authority, which is true of a store only this machine writes and
        false of a shared cluster graph.
    """
    from . import repo as repo_module

    # Listing works against a deployment; pruning does not. It compares store
    # branches against this machine's git refs and then deletes — neither of
    # which a deployed replica's stores have any business following.
    if prune and _is_remote():
        print(
            "--prune deletes branches from the LOCAL stores, which a deployed "
            "witan service does not share. Unset WITAN_REMOTE_URL to prune."
        )
        raise SystemExit(1)

    rows = _fn(_srv().code_indexed_branches)(branch=branch)
    if not rows:
        print("No indexed repositories.")
        return

    current_slug = repo_module.detect()
    git_branches = repo_module.local_branches() if prune else None

    for row in rows:
        repo_uri, found = row["repo"], row["views"]
        if found is None:
            print(f"{repo_uri}: <error: store could not be listed>")
            continue
        names = [v["view"] for v in found]
        print(f"{repo_uri}: main" + ("," + ",".join(names) if names else ""))

        if not (prune and repo_uri == current_slug and git_branches is not None):
            continue
        client = _branch_client(repo_uri)
        # A shared cluster graph's branches are not this machine's git refs to
        # follow. "This checkout doesn't have that branch" and "that branch is
        # gone" are indistinguishable from here, so one user pruning would
        # delete another user's in-flight branch view. Distinct from the
        # `_is_remote()` check above: that one is about where the READ tools
        # dispatch, this one about where the STORE lives — either can be remote
        # without the other.
        if client.is_remote:
            print(
                f"{repo_uri}: refusing to prune a shared graph — its branches "
                "belong to every user of it, not to this checkout's git refs."
            )
            continue
        # Match on the branch component, not the view name: a view carries its
        # writer as well, and `git_branches` only knows about branches.
        for view in found:
            gone = view["branch"] not in git_branches
            if view["branch"] == repo_module.DETACHED_BRANCH or gone:
                client.delete_branch(view["view"])
                print(f"  pruned {view['view']}")


def _branch_client(repo_uri: str):
    """A client on ``repo_uri``'s store, for branch listing/deletion."""
    from . import config as cfg_module
    from . import store as store_module

    cfg = cfg_module.load()
    return store_module.store_for_repo(repo_uri, cfg).client(cfg)


# ── Indexed repositories ─────────────────────────────────────────────────────


@app.command
def repos() -> None:
    """List the repositories that have a code graph indexed."""
    from rich.console import Console

    rows = _fn(_srv().code_indexed_repos)()
    if not rows:
        Console().print("[dim]No indexed repositories.[/dim]")
        return

    table_rows: list[dict[str, object]] = [
        {
            "repo": r["repo"],
            "files": "?" if r["files"] is None else str(r["files"]),
            # Both are null for a cluster graph — they describe a store
            # directory, which a client of a shared graph does not have.
            "size": _human_size(r["bytes"]),
            # The tool returns an epoch, so a remote deployment's stores render
            # in the reader's timezone rather than the server's.
            "last indexed": _human_time(r["last_indexed"]),
        }
        for r in rows
    ]
    _render_table(
        title="Indexed repositories",
        columns=["repo", "files", "size", "last indexed"],
        rows=table_rows,
        no_wrap={"files", "size", "last indexed"},
    )


# ── Remote auth (ADR 0005, path a) ───────────────────────────────────────────


def _remote_or_exit():
    from . import config as cfg_module

    try:
        remote = cfg_module.load_remote_config()
    except ValueError as exc:
        print(exc)
        raise SystemExit(1) from None
    if remote is None:
        print(
            "Remote mode is not configured. Set WITAN_REMOTE_URL (and "
            "WITAN_OIDC_ISSUER) to point the CLI at a deployed witan service."
        )
        raise SystemExit(1)
    return remote


@app.command
def login() -> None:
    """Authenticate to the deployed witan service via the OIDC device grant.

    Prints a verification URL and a user code; approve it in a browser, and the
    resulting token is cached (mode 0600) and refreshed automatically for
    subsequent `witan-code …` commands.

    The cache is shared with the `witan` CLI and keyed by (issuer, client id),
    so if you already ran `witan login` against the same deployment you do not
    need this at all — and running it here also logs `witan` in.
    """
    from rich.console import Console

    from .remote import oidc

    console = Console()
    remote = _remote_or_exit()

    def _prompt(device: dict) -> None:
        complete = device.get("verification_uri_complete")
        uri = device.get("verification_uri", "")
        code = device.get("user_code", "")
        console.print("\n[bold]Authenticate witan-code CLI[/bold]")
        if complete:
            console.print(f"  Open: [cyan underline]{complete}[/cyan underline]")
        console.print(
            f"  Or go to [cyan underline]{uri}[/cyan underline] and enter "
            f"code [bold]{code}[/bold]\n  Waiting for approval…"
        )

    try:
        claims = oidc.login(remote, on_prompt=_prompt)
    except oidc.RemoteAuthError as exc:
        console.print(f"[red]Login failed:[/red] {exc}")
        raise SystemExit(1) from None
    who = claims.get("preferred_username") or claims.get("sub", "?")
    console.print(f"[green]Logged in[/green] as [bold]{who}[/bold] → {remote.url}")


@app.command
def logout() -> None:
    """Forget the cached token for the configured deployment.

    The cache is shared with the `witan` CLI, so this logs both out.
    """
    from rich.console import Console

    from .remote import oidc

    console = Console()
    remote = _remote_or_exit()
    if oidc.logout(remote):
        console.print(f"[green]Logged out[/green] of {remote.url}")
    else:
        console.print("[yellow]No cached session to clear.[/yellow]")


@app.command
def whoami() -> None:
    """Show the identity the CLI presents to the deployed witan service."""
    from datetime import datetime, timezone

    from rich.console import Console

    from .remote import oidc

    console = Console()
    remote = _remote_or_exit()
    try:
        token = oidc.get_valid_token(remote)
    except oidc.NeedsLogin as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise SystemExit(1) from None
    claims = oidc.decode_claims(token)
    if remote.target_name:
        console.print(f"[bold]Target[/bold]    {remote.target_name}")
    console.print(f"[bold]Endpoint[/bold]  {remote.url}")
    console.print(f"[bold]User[/bold]      {claims.get('preferred_username', '?')}")
    if claims.get("email"):
        console.print(f"[bold]Email[/bold]     {claims['email']}")
    console.print(f"[bold]sub[/bold]       {claims.get('sub', '')}")
    exp = claims.get("exp")
    if exp:
        when = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
        console.print(f"[bold]Expires[/bold]   {when}")


def _human_time(epoch: float | None) -> str:
    import datetime

    if epoch is None:
        return "?"
    return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def _human_size(n: int | None) -> str:
    if n is None:
        return "?"
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
    # ★ EVERY command, not just `serve`. Until this was here, `serve` was the
    # only entry point that configured observability — so the CI indexer, which
    # runs `witan code index`, had no Sentry client at all and no amount of
    # log-level correctness at a call site could have reported anything.
    #
    # It also puts structlog on the stdlib pipeline (`stdlib.LoggerFactory`),
    # which is what Sentry's LoggingIntegration hooks; the unconfigured fallback
    # writes straight to stderr and is invisible to it.
    #
    # `instrument=False` because this is a short-lived CLI: the OTel
    # auto-instrumentors are worth their startup cost in a server process and
    # not in `witan code repos`. Everything here no-ops without its env var —
    # no SENTRY_DSN, no client — so a developer pays nothing.
    from witan_core.observability import configure_observability

    configure_observability(instrument=False)
    app(tokens)


def cli() -> None:
    from rich.console import Console

    from .remote.oidc import RemoteAuthError
    from .remote.proxy import (
        RemoteCredentialRejected,
        RemoteToolFailed,
        RemoteToolUnavailable,
        RemoteUnreachable,
    )
    from .remote.store import RemotePayloadTooLarge

    try:
        app.meta()
    # Same remote failures the `witan` entrypoint classifies. This CLI had no
    # guard at all, so a deployment that was down, or a token the server
    # rejected, printed a traceback out of `witan-code symbols`.
    # RemotePayloadTooLarge comes from the store session rather than the proxy:
    # this CLI's oversized bodies are index batches, not tool arguments.
    # RemoteToolFailed covers the tool running and refusing — a Cedar denial on
    # a code graph the caller may not read, or a branch the cluster does not
    # have.
    except (
        RemoteAuthError,
        RemoteCredentialRejected,
        RemotePayloadTooLarge,
        RemoteToolFailed,
        RemoteToolUnavailable,
        RemoteUnreachable,
    ) as exc:
        # markup=False — a target block is written `[qa]`, which rich parses as
        # a style tag and swallows, taking the name of the setting to unset
        # with it.
        Console().print(str(exc), style="red", markup=False)
        raise SystemExit(1) from None


if __name__ == "__main__":
    cli()
