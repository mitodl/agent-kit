import asyncio
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal

from fastmcp import Context, FastMCP

from . import bridge_extractors
from . import config as cfg_module
from . import elicit
from . import indexer
from . import repo as repo_module
from . import stitch
from . import store as store_module
from .graph import OmnigraphClient

# ── Startup ───────────────────────────────────────────────────────

cfg = cfg_module.load()

mcp = FastMCP(
    "witan-code",
    instructions=(
        "Tree-sitter code graph across one or many repositories: resolve symbol "
        "definitions, callers, references, and change-impact, and trace cross-repo "
        "contracts (env vars, endpoints, packages, services).\n\n"
        "Symbol ids have the form `<repo>#<path/to/file.py>::<QualifiedName>` "
        "(a whole module is `<repo>#<path/to/file.py>::<module>`). Name-based "
        "tools return this id in the `symbol_id` field; pass it straight back to "
        "the id-routed tools (code_find_references / code_callers / code_impact / "
        "code_cross_repo_impact). Symbol ids compose with witan memory via soft "
        "references.\n\n"
        "Repo scoping: tools auto-detect the current repo from `.git/config`. Pass "
        "`repo` (a canonical URI like `https://github.com/mitodl/ol-django`) to "
        "scope to one repo, or omit it when outside any repo to fan out across "
        "every indexed repo.\n\n"
        "Branch semantics: name-routed tools (code_find_definition, "
        "code_search_symbol, code_symbols_in_file) accept `branch`. The default "
        "follows the current checkout's branch ONLY when querying the current "
        "detected repo; when you pass `repo` for a different repo and omit "
        "`branch`, you read that store's default (main) view — pass `branch` "
        "explicitly to target another view. Id-routed tools (code_find_references "
        "/ callers / impact / cross_repo_impact) take no `branch`: `symbol_id` "
        "does not encode a branch, so they read the default view of the id's "
        "repo store.\n\n"
        "min_precision (`heuristic` default | `precise`) on the interface and "
        "cross-repo tools: `precise` keeps only edges also confirmed by a "
        "canonical-symbol join, suppressing false positives.\n\n"
        "Calls/References edges are heuristic (syntactic name resolution), not a "
        "precise call graph; code_find_references includes code_callers."
    ),
)

# Carries `elicit.confirm`/`elicit.text` asks over MCP 2026-07-28, which has no
# server→client back-channel to run them on. Inert on the handshake eras.
mcp.add_middleware(elicit.MRTRElicitationMiddleware())

# The `io.modelcontextprotocol/tasks` extension (SEP-2663), which lets a client
# take a handle on a long `code_reindex` and poll it instead of holding a tool
# call open for minutes. Optional (`witan-code[tasks]`): it exists only for
# fastmcp 4.x, and this package still supports the 3.4.x end of its pin. The
# flag has to gate the tool declaration too — a `task=True` tool refuses to
# serve at all when the extension is missing, rather than degrading.
#
# Note the backend defaults to in-process `memory://`, which is what a per-repo
# stdio server wants. A multi-replica deployment polling through a round-robin
# LB would need a shared Docket backend (FASTMCP_DOCKET_URL) — moot today, since
# indexing needs a git checkout the deployment doesn't have.
try:
    from fastmcp_tasks import TasksExtension
except ImportError:
    TASKS_ENABLED = False
else:
    TASKS_ENABLED = True
    mcp.add_extension(TasksExtension())

# Client cache keyed by "store path|branch" ("" = main).
_clients: dict[str, OmnigraphClient] = {}

# Known branch sets per store, with a short TTL: a cache miss re-lists (so a
# branch created after server start appears immediately) and expiry bounds how
# long a deleted branch (e.g. `branches --prune`) keeps routing reads before
# they degrade back to main.
_store_branches: dict[str, tuple[float, frozenset[str]]] = {}
_BRANCH_CACHE_TTL = 30.0

# repo_module.detect()/store_branch() spawn git subprocesses, and every
# per-repo AND bridge read that follows "the current checkout" calls them.
# A short TTL amortizes a burst of tool calls within one agent turn (the
# common case) while still picking up a branch switch within a couple
# seconds — long-lived enough to matter for latency, short enough that no
# test needs to know about it (git state changes mid-test would otherwise
# read stale for the TTL window; tests that switch branches mid-test must
# call ``_git_context.clear()``).
_git_context: dict[str, tuple[float, str | None]] = {}
_GIT_CONTEXT_TTL = 2.0


def _cached_git(key: str, fn) -> str | None:
    cached = _git_context.get(key)
    if cached is not None:
        stamp, val = cached
        if time.monotonic() - stamp < _GIT_CONTEXT_TTL:
            return val
    val = fn()
    _git_context[key] = (time.monotonic(), val)
    return val


def _cached_detect() -> str | None:
    return _cached_git("detect", repo_module.detect)


def _cached_store_branch() -> str | None:
    return _cached_git("store_branch", repo_module.store_branch)


def _client_for_path(path, branch: str | None = None) -> OmnigraphClient:
    key = f"{path}|{branch or ''}"
    if key not in _clients:
        _clients[key] = OmnigraphClient(str(path), cfg.queries_dir, branch=branch)
    return _clients[key]


def _branch_in_store(path, branch: str) -> bool:
    key = str(path)
    cached = _store_branches.get(key)
    if cached is not None:
        stamp, branches = cached
        if time.monotonic() - stamp < _BRANCH_CACHE_TTL and branch in branches:
            return True
    try:
        branches = frozenset(_client_for_path(path).list_branches())
    except Exception:  # noqa: BLE001 — degrade to main on any listing failure
        return False
    _store_branches[key] = (time.monotonic(), branches)
    return branch in branches


def _resolve_branch(store, repo: str, requested: str | None) -> str | None:
    """Effective omnigraph branch for a read: None = the store's main branch.

    An explicit ``requested`` branch is used when it exists in the store
    (else main — degrade, don't error). With no request, a query against the
    *current* repo follows the checkout's branch so an agent working on a
    feature branch sees its own in-flight view by default.
    """
    if requested:
        # Same git→store mapping as indexing, so a request for branch "main"
        # in a master-default repo routes to its "_main" store branch rather
        # than the store's default view.
        b = repo_module.branch_store_name(requested)
        return b if _branch_in_store(store, b) else None
    if repo == _cached_detect():
        b = _cached_store_branch()
        if b and _branch_in_store(store, b):
            return b
    return None


def _client_for_repo(repo: str, branch: str | None = None) -> OmnigraphClient | None:
    """Client for a specific repo's store (origin scoping), or None if absent."""
    store = store_module.store_for_repo(repo, cfg)
    if not store.exists():
        return None
    return _client_for_path(store, _resolve_branch(store, repo, branch))


def _client_for_symbol(symbol_id: str) -> OmnigraphClient | None:
    """Route a `repo#path::Name` id to the store of its repo prefix."""
    return _client_for_repo(symbol_id.split("#", 1)[0])


def _all_clients() -> list[OmnigraphClient]:
    """A client per indexed per-repo store (excludes the shared bridge store)."""
    if not cfg.code_dir.exists():
        return []
    bridge = store_module.bridge_store(cfg).name
    return [
        _client_for_path(p)
        for p in sorted(cfg.code_dir.glob("*.omni"))
        if p.name != bridge
    ]


def _resolve_clients(
    repo: str | None, branch: str | None = None
) -> list[OmnigraphClient]:
    """Stores to query: the named repo → the current repo → all repos (fan-out).

    Rows already carry their `repo`, so fan-out results are self-tagging.
    ``branch`` scopes the single-repo cases; fan-out always reads main.
    """
    if repo:
        client = _client_for_repo(repo, branch)
        return [client] if client else []
    slug = _cached_detect()
    if slug:
        client = _client_for_repo(slug, branch)
        if client is not None:
            return [client]
    return _all_clients()


def _bridge_branch_name(repo: str, branch: str) -> str:
    return f"{cfg_module.sanitize_slug(repo)}/{branch}"


def _resolve_bridge_branch(store, repo: str | None) -> str | None:
    """Effective bridge branch for reads scoped to ``repo``: None = bridge main.

    Mirrors ``_resolve_branch``'s "current repo only" rule: the overlay is a
    *specific* repo's in-flight bindings on top of everyone else's main
    (docs/BRANCH_INDEXING.md § Bridge store), so it only applies
    automatically when ``repo`` is the checkout's own detected repo — an
    agent asking about some other repo's symbol while sitting elsewhere
    should not silently pick up an unrelated overlay branch.
    """
    if repo is None or repo != _cached_detect():
        return None
    branch = _cached_store_branch()
    if not branch:
        return None
    bridge_branch = _bridge_branch_name(repo, branch)
    return bridge_branch if _branch_in_store(store, bridge_branch) else None


def _bridge_client(repo: str | None = None) -> OmnigraphClient | None:
    """Resolve the shared cross-repo bridge client, or None if not built yet.

    When ``repo`` is omitted, auto-detects the current checkout's repo. Reads
    are scoped to that repo's bridge branch overlay when it's on a
    non-default git branch that has already been written to the bridge;
    otherwise (no repo context, on the default branch, or nothing written to
    that branch's overlay yet) reads see bridge main.
    """
    store = store_module.bridge_store(cfg)
    if not store.exists():
        return None
    if repo is None:
        repo = _cached_detect()
    return _client_for_path(store, _resolve_bridge_branch(store, repo))


async def _confirm_and_reindex(
    ctx: Context | None, repo: str
) -> OmnigraphClient | None:
    """Offer to index ``repo`` now if its store is missing AND it's the repo
    we're actually sitting in (code_reindex has no way to index anything
    else). Returns a fresh client on an accepted+successful index, else None
    — callers must fall back to their existing shaped-empty return exactly as
    before elicitation existed.
    """
    if repo != _cached_detect():
        return None
    ok = await elicit.confirm(
        ctx,
        f"No code graph indexed yet for {repo}. Index it now? "
        "(may take a while on a large repo)",
        default_when_unsupported=False,
        title="Index now?",
    )
    if not ok:
        return None
    target = repo_module.root() or Path.cwd()
    try:
        await asyncio.to_thread(indexer.index_path, target, force=False, config=cfg)
    except Exception:  # noqa: BLE001 — indexing failure degrades like a missing store
        return None
    return _client_for_repo(repo)


async def _confirm_and_reindex_bridge(ctx: Context | None) -> OmnigraphClient | None:
    """Offer to index the CURRENT repo when the shared cross-repo bridge store
    doesn't exist at all yet (indexing any repo creates/populates it). Same
    additive fallback contract as ``_confirm_and_reindex``.
    """
    repo = _cached_detect()
    if repo is None:
        return None
    ok = await elicit.confirm(
        ctx,
        f"No cross-repo graph indexed yet. Index the current repo ({repo}) "
        "now to start building it?",
        default_when_unsupported=False,
        title="Index now?",
    )
    if not ok:
        return None
    target = repo_module.root() or Path.cwd()
    try:
        await asyncio.to_thread(indexer.index_path, target, force=False, config=cfg)
    except Exception:  # noqa: BLE001 — indexing failure degrades like a missing store
        return None
    return _bridge_client()


def _fan_out(clients: list[OmnigraphClient], fn) -> list[dict]:
    """Run ``fn(client) -> list[dict]`` across clients in parallel threads.

    Falls back to a simple loop for 0–1 clients to avoid thread-pool overhead
    on the common single-repo case.
    """
    if len(clients) <= 1:
        return [row for c in clients for row in fn(c)]
    out: list[dict] = []
    errors: list[Exception] = []
    with ThreadPoolExecutor(max_workers=min(len(clients), 8)) as pool:
        futures = {pool.submit(fn, c): c for c in clients}
        for f in as_completed(futures):
            try:
                out.extend(f.result())
            except Exception as exc:
                errors.append(exc)
    if errors and not out:
        raise errors[0]
    return out


def _as_symbol_id(rows: list[dict]) -> list[dict]:
    """Rename the identifier field `slug` → `symbol_id` on returned Symbol rows.

    Queries project the Symbol's identifier as `slug`, but every tool that
    consumes an id names its parameter `symbol_id`. Renaming at the output
    boundary makes the value round-trip under one name (definition → references)
    without changing the stored field or the internal traversal code.
    """
    return [{"symbol_id" if k == "slug" else k: v for k, v in r.items()} for r in rows]


def _file_id(path: str, repo: str) -> str:
    """Build the CodeFile id (`repo#relpath`) for a repo-relative or abs path."""
    base = repo_module.root() or Path.cwd()
    p = Path(path)
    try:
        rel = (
            p.resolve().relative_to(base.resolve()).as_posix()
            if p.is_absolute() or p.exists()
            else p.as_posix()
        )
    except ValueError:
        rel = p.as_posix()
    return f"{repo}#{rel}"


# ── Tools ─────────────────────────────────────────────────────────


@mcp.tool
async def code_find_definition(
    name: str,
    repo: str | None = None,
    branch: str | None = None,
    ctx: Context | None = None,
) -> list[dict]:
    """
    Find symbol definitions whose name or qualified name matches ``name``.

    Returns matching symbols (function, method, class, module, …) with their
    ``symbol_id``, file, line range, signature, docstring, and ``repo`` of
    origin. Feed a returned ``symbol_id`` to code_find_references / code_callers
    / code_impact.

    Parameters
    ----------
    name:
        Bare name (``run``) or qualified name (``Service.run``).
    branch:
        Git branch whose indexed view to query (e.g. another agent's in-flight
        branch). Defaults to the checkout's branch when querying the current
        repo; when ``repo`` names a different repo and ``branch`` is omitted,
        reads that store's default (main) view.
    """

    def _query(client: OmnigraphClient) -> list[dict]:
        rows = client.read(
            "code_read.gq", "find_by_qualified_name", {"qualified_name": name}
        )
        return rows or client.read("code_read.gq", "find_by_name", {"name": name})

    matches = _as_symbol_id(_fan_out(_resolve_clients(repo, branch), _query))

    if repo is None:
        repos = sorted({m["repo"] for m in matches if m.get("repo")})
        if len(repos) > 1:
            chosen = await elicit.choose_repo(
                ctx,
                f"'{name}' matches in {len(repos)} repos: {', '.join(repos)}. "
                "Reply with one repo URI to narrow the results, or leave blank "
                "to see every match.",
                repos,
            )
            if chosen is not None:
                matches = [m for m in matches if m.get("repo") == chosen]

    return matches


@mcp.tool
async def code_find_references(
    symbol_id: str, ctx: Context | None = None
) -> list[dict]:
    """
    Find symbols that reference or call ``symbol_id`` (incoming edges).

    Supersets code_callers (references includes calls); use code_callers when
    you want calls only. Heuristic — may miss or over-report.
    """
    client = _client_for_symbol(symbol_id)
    if client is None:
        client = await _confirm_and_reindex(ctx, symbol_id.split("#", 1)[0])
        if client is None:
            return []
    refs = client.read("code_read.gq", "referencers", {"id": symbol_id})
    callers = client.read("code_read.gq", "callers", {"id": symbol_id})
    seen = {r["slug"] for r in refs}
    return _as_symbol_id(refs + [c for c in callers if c["slug"] not in seen])


@mcp.tool
async def code_callers(symbol_id: str, ctx: Context | None = None) -> list[dict]:
    """
    Find symbols that call ``symbol_id`` (incoming Calls edges).

    Heuristic name-resolution based; not a precise call graph. For calls plus
    other references use code_find_references.
    """
    client = _client_for_symbol(symbol_id)
    if client is None:
        client = await _confirm_and_reindex(ctx, symbol_id.split("#", 1)[0])
        if client is None:
            return []
    return _as_symbol_id(client.read("code_read.gq", "callers", {"id": symbol_id}))


@mcp.tool
async def code_impact(
    symbol_id: str,
    max_depth: int = 5,
    max_nodes: int = 200,
    ctx: Context | None = None,
) -> dict:
    """
    Estimate change impact: the transitive set of callers of ``symbol_id``.

    Performs a breadth-first traversal over Calls edges, capping at ``max_depth``
    levels and ``max_nodes`` total. Returns the reached symbols and whether the
    traversal was truncated. Heuristic — see code_callers.

    Parameters
    ----------
    max_depth:
        Maximum BFS depth (default 5).
    max_nodes:
        Cap on total symbols returned (default 200).
    """
    if _client_for_symbol(symbol_id) is None:
        if await _confirm_and_reindex(ctx, symbol_id.split("#", 1)[0]) is None:
            return {"root": symbol_id, "impacted": [], "truncated": False}

    visited: dict[str, dict] = {}
    frontier = [symbol_id]
    truncated = False
    depth = 0

    while frontier and depth < max_depth:
        next_frontier: list[str] = []
        for sid in frontier:
            # Route each node to its repo's store, so impact can cross repos.
            client = _client_for_symbol(sid)
            if client is None:
                continue
            for caller in client.read("code_read.gq", "callers", {"id": sid}):
                cid = caller["slug"]
                if cid in visited or cid == symbol_id:
                    continue
                if len(visited) >= max_nodes:
                    truncated = True
                    break
                caller["depth"] = depth + 1
                visited[cid] = caller
                next_frontier.append(cid)
            if truncated:
                break
        if truncated:
            break
        frontier = next_frontier
        depth += 1

    if frontier and depth >= max_depth:
        truncated = True

    return {
        "root": symbol_id,
        "impacted": _as_symbol_id(list(visited.values())),
        "truncated": truncated,
    }


@mcp.tool
async def code_symbols_in_file(
    path: str, repo: str | None = None, ctx: Context | None = None
) -> list[dict]:
    """
    List symbols defined in ``path`` (repo-relative or absolute).

    A file is inherently repo-local, so this resolves a single repo (no fan-out):
    the given ``repo`` or, if omitted, the current repo.

    Parameters
    ----------
    path:
        Source file path.
    repo:
        Canonical repo URI the file belongs to. Defaults to the current repo.
    """
    slug = repo or _cached_detect()
    if slug is None:
        slug = await elicit.text(
            ctx,
            "No repo detected for this file (not in a git repo, or no remote). "
            "Enter its canonical repo URI (e.g. https://github.com/org/name):",
            default="",
            title="Repo URI",
        )
        if not slug:
            return []
    client = _client_for_repo(slug)
    if client is None:
        client = await _confirm_and_reindex(ctx, slug)
        if client is None:
            return []
    return _as_symbol_id(
        client.read(
            "code_read.gq", "symbols_in_file", {"file_id": _file_id(path, slug)}
        )
    )


SymbolKind = Literal[
    "function",
    "method",
    "class",
    "module",
    "variable",
    "interface",
    "type",
    "enum",
    "key",
    "table",
    "cte",
    "block",
]


@mcp.tool
def code_search_symbol(
    query: str,
    kind: SymbolKind | None = None,
    repo: str | None = None,
    branch: str | None = None,
) -> list[dict]:
    """
    Full-text/substring search over symbol qualified names (BM25-ranked).

    Use to locate a symbol when you only remember part of its name. Each result
    carries its ``symbol_id`` and ``repo`` of origin. BM25 ranking is per-store;
    cross-repo fan-out concatenates each store's ranked results.

    Parameters
    ----------
    query:
        Search terms matched against ``qualified_name``.
    kind:
        Optional filter to a single symbol kind: ``function``, ``method``,
        ``class``, ``module``, ``variable``, ``interface``, ``type``, ``enum``,
        ``key``, ``table``, ``cte``, or ``block``. Pass e.g. ``kind="function"``
        to exclude the many YAML ``key`` symbols when searching for code.
    branch:
        Git branch whose indexed view to query. Defaults to the checkout's
        branch when querying the current repo; when ``repo`` names a different
        repo and ``branch`` is omitted, reads that store's default (main) view.
    """

    def _query(client: OmnigraphClient) -> list[dict]:
        if kind:
            return client.read(
                "code_read.gq", "search_symbols_by_kind", {"query": query, "kind": kind}
            )
        return client.read("code_read.gq", "search_symbols", {"query": query})

    return _as_symbol_id(_fan_out(_resolve_clients(repo, branch), _query))


BindingKind = Literal["env_var", "package", "service", "endpoint"]
PrecisionTier = Literal["precise", "heuristic", "fuzzy"]


def _precise_pairs(repo: str | None = None) -> frozenset:
    """(consumer_repo, provider_repo, kind, key_norm) covered by a Stage-2 join."""
    client = _bridge_client(repo=repo)
    if client is None:
        return frozenset()
    from . import edges as edges_module

    rows = client.read("bridge.gq", "all_repo_symbols", {})
    return edges_module.precise_pairs(rows)


def _filter_by_precision(rows: list[dict], min_precision: str) -> list[dict]:
    if not rows or min_precision != "precise":
        return rows
    key_norms = {(kind, key_norm) for _, _, kind, key_norm in _precise_pairs()}
    return [r for r in rows if (r.get("kind"), r.get("key_norm")) in key_norms]


@mcp.tool
async def code_interface_providers(
    kind: BindingKind,
    key: str,
    min_precision: PrecisionTier = "heuristic",
    ctx: Context | None = None,
) -> list[dict]:
    """
    Find repos that PROVIDE a cross-repo contract ``key`` of ``kind``.

    Spans every indexed repo via the shared bridge store. Examples:
    ``code_interface_providers("env_var", "MITOL_APP_BASE_URL")`` returns the
    ol-infrastructure binding that sets it; ``("endpoint", "/api/v1/courses/")``
    returns the Django/OpenAPI route that serves it.

    Parameters
    ----------
    kind:
        ``env_var``, ``package``, ``service``, or ``endpoint``.
    key:
        The contract value. For ``endpoint`` a raw path is accepted and
        normalized (path params collapse to ``{}``).
    min_precision:
        ``heuristic`` (default) | ``precise`` — see server instructions.
    """
    return await _bindings_by_role(kind, key, "provider", min_precision, ctx)


@mcp.tool
async def code_interface_consumers(
    kind: BindingKind,
    key: str,
    min_precision: PrecisionTier = "heuristic",
    ctx: Context | None = None,
) -> list[dict]:
    """
    Find repos that CONSUME a cross-repo contract ``key`` of ``kind``.

    The mirror of ``code_interface_providers`` — e.g. which repos read an env var
    or call an API endpoint. Spans every indexed repo.

    Parameters
    ----------
    kind:
        ``env_var``, ``package``, ``service``, or ``endpoint``.
    key:
        The contract value (``endpoint`` paths are normalized).
    min_precision:
        ``heuristic`` (default) | ``precise`` — see server instructions.
    """
    return await _bindings_by_role(kind, key, "consumer", min_precision, ctx)


async def _bindings_by_role(
    kind: str,
    key: str,
    role: str,
    min_precision: str = "heuristic",
    ctx: Context | None = None,
) -> list[dict]:
    client = _bridge_client()
    if client is None:
        client = await _confirm_and_reindex_bridge(ctx)
        if client is None:
            return []
    key_norm = bridge_extractors.normalize_key(kind, key)
    rows = client.read(
        "bridge.gq",
        "bindings_by_key_role",
        {"kind": kind, "key_norm": key_norm, "role": role},
    )
    return _filter_by_precision(rows, min_precision)


@mcp.tool
async def code_cross_repo_impact(
    symbol_id: str,
    min_precision: PrecisionTier = "heuristic",
    ctx: Context | None = None,
) -> dict:
    """
    Find the cross-repo surface of a symbol.

    Looks up the interface bindings attributed to ``symbol_id`` (env vars it
    reads, packages it imports, endpoints it serves/calls), then returns every
    binding for those same contracts in OTHER repos — i.e. who else is coupled to
    this symbol across the SOA. Returns a shaped empty result when the symbol has
    no cross-repo surface or the bridge store does not exist yet.

    Generic env vars (DEBUG, PORT, …) are flagged ``generic`` and excluded from
    the cross-repo fan-out so a trivial edit doesn't appear to touch every repo.

    Parameters
    ----------
    min_precision:
        ``heuristic`` (default) | ``precise`` — see server instructions.
    """
    empty = {"symbol_id": symbol_id, "bindings": [], "cross_repo": []}
    own_repo = symbol_id.split("#", 1)[0]
    # Scope the bridge read to the SYMBOL's own repo, not blindly the cwd:
    # _bridge_client only applies an overlay when repo matches the checkout
    # (it can't discover another repo's branch state from here regardless),
    # so this just avoids picking up an unrelated overlay from the caller's
    # own cwd when asking about a symbol in some other repo.
    client = _bridge_client(repo=own_repo)
    if client is None:
        client = await _confirm_and_reindex_bridge(ctx)
        if client is None:
            return empty

    own = client.read("bridge.gq", "bindings_for_symbol", {"symbol_id": symbol_id})
    if not own:
        return empty

    pairs = _precise_pairs(repo=own_repo) if min_precision == "precise" else None
    seen: set[str] = set()
    cross: list[dict] = []
    for b in own:
        if b.get("generic"):
            continue
        for other in client.read(
            "bridge.gq",
            "bindings_by_key",
            {"kind": b["kind"], "key_norm": b["key_norm"]},
        ):
            if other["repo"] == own_repo or other["slug"] in seen:
                continue
            if pairs is not None and not (
                (own_repo, other["repo"], b["kind"], b["key_norm"]) in pairs
                or (other["repo"], own_repo, b["kind"], b["key_norm"]) in pairs
            ):
                continue
            seen.add(other["slug"])
            cross.append(other)
    return {"symbol_id": symbol_id, "bindings": own, "cross_repo": cross}


@mcp.tool
async def code_precise_edges(
    repo: str | None = None, ctx: Context | None = None
) -> list[dict]:
    """
    Precise cross-repo edges resolved by canonical symbol string.

    Joins every repo's unresolved external-symbol references against other
    repos' exported symbols — a read-time join, distinct from the coarser
    (kind, key_norm) heuristic grouping ``code_interface_*`` use. Each edge
    carries ``match_count`` (how many providers a reference joined to) and
    ``ambiguous_version`` (true when more than one provider survives version
    disambiguation); filter to ``preferred`` edges to narrow a fan-out to its
    best candidate(s). A reference with no precise match shows up in
    ``code_unresolved_symbols`` instead.

    Parameters
    ----------
    repo:
        Keep only edges whose consumer OR provider is this repo. Omit to see
        every precise edge in the bridge store.
    """
    client = _bridge_client(repo=repo)
    if client is None:
        client = await _confirm_and_reindex_bridge(ctx)
        if client is None:
            return []
    rows = client.read("bridge.gq", "all_repo_symbols", {})
    edges, _ = stitch.resolve(rows)
    return [
        e.as_dict()
        for e in edges
        if repo is None or repo in (e.consumer_repo, e.provider_repo)
    ]


@mcp.tool
async def code_unresolved_symbols(
    repo: str | None = None, ctx: Context | None = None
) -> list[dict]:
    """
    External symbol references with no precise cross-repo match.

    Surfaces indexing-coverage gaps: a repo consumes a contract (env var,
    package, endpoint, service) that no indexed repo currently exports —
    either the provider repo isn't indexed yet, or the reference genuinely has
    no provider in this SOA. These still get a heuristic-tier chance via
    ``code_interface_consumers``/``_providers``; this tool finds what's NOT
    precisely resolved.

    Parameters
    ----------
    repo:
        Keep only unresolved references from this consumer repo. Omit to see
        every unresolved reference in the bridge store.
    """
    client = _bridge_client(repo=repo)
    if client is None:
        client = await _confirm_and_reindex_bridge(ctx)
        if client is None:
            return []
    rows = client.read("bridge.gq", "all_repo_symbols", {})
    _, unresolved = stitch.resolve(rows)
    return [r for r in unresolved if repo is None or r["repo"] == repo]


@mcp.tool
async def code_interface_search(
    query: str,
    kind: BindingKind | None = None,
    min_precision: PrecisionTier = "heuristic",
    ctx: Context | None = None,
) -> list[dict]:
    """
    BM25 search over cross-repo interface bindings (by normalized key).

    Use to discover contract keys when you only remember part of a name —
    e.g. ``code_interface_search("BASE_URL")`` or ``("courses", kind="endpoint")``.

    Parameters
    ----------
    query:
        Search terms matched against the normalized key.
    kind:
        Optional filter to one kind.
    min_precision:
        ``heuristic`` (default) | ``precise`` — see server instructions.
    """
    client = _bridge_client()
    if client is None:
        client = await _confirm_and_reindex_bridge(ctx)
        if client is None:
            return []
    if kind:
        rows = client.read(
            "bridge.gq", "search_bindings_by_kind", {"query": query, "kind": kind}
        )
    else:
        rows = client.read("bridge.gq", "search_bindings", {"query": query})
    return _filter_by_precision(rows, min_precision)


@mcp.tool(task=TASKS_ENABLED)
async def code_reindex(path: str | None = None, force: bool = False) -> dict:
    """
    Index (or re-index) the current repo, or a subpath of it.

    Incremental by default — unchanged files (matching content hash) are
    skipped. Lazily creates the per-repo store on first run. Returns a summary of
    files scanned/indexed/skipped and symbols/edges written.

    A full rebuild runs for minutes on a large repo, so this tool accepts
    task-augmented execution (`io.modelcontextprotocol/tasks`): a client that
    asks for it gets a task handle back immediately and polls `tasks/get`,
    instead of holding one tool call open for the whole index. Asking is the
    client's choice — omit it and the call runs to completion as it always has.

    Parameters
    ----------
    path:
        Optional file or directory under the repo. Defaults to the repo root
        (or cwd if not in a git repo).
    force:
        Re-index every file regardless of content hash.
    """
    target = Path(path) if path else (repo_module.root() or Path.cwd())
    # Off the event loop: indexing is CPU- and IO-bound and would otherwise
    # stall every other request on this server for its whole run.
    stats = await asyncio.to_thread(indexer.index_path, target, force=force, config=cfg)
    return {
        "path": str(target),
        "scanned": stats.scanned,
        "indexed": stats.indexed,
        "skipped": stats.skipped,
        "symbols": stats.symbols,
        "edges": stats.edges,
        "bindings": stats.bindings,
        "errors": stats.errors,
    }
