import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal

from fastmcp import FastMCP

from . import bridge_extractors
from . import config as cfg_module
from . import indexer
from . import repo as repo_module
from . import store as store_module
from .graph import OmnigraphClient

# ── Startup ───────────────────────────────────────────────────────

cfg = cfg_module.load()

mcp = FastMCP(
    "witan-code",
    instructions=(
        "Tree-sitter code graph. Resolve symbol definitions, callers, references, "
        "and call-impact across one or many repositories. Pass `repo` to scope a "
        "lookup to a single repo's store; omit it to use the current repo, or to "
        "fan out across every indexed repo when not inside one. Symbol ids are "
        "`repo#path::QualifiedName` and compose with witan memory via soft "
        "references. Calls/References edges are heuristic (syntactic name "
        "resolution), not a precise call graph."
    ),
)

# Client cache keyed by "store path|branch" ("" = main).
_clients: dict[str, OmnigraphClient] = {}

# Known branch sets per store, with a short TTL: a cache miss re-lists (so a
# branch created after server start appears immediately) and expiry bounds how
# long a deleted branch (e.g. `branches --prune`) keeps routing reads before
# they degrade back to main.
_store_branches: dict[str, tuple[float, frozenset[str]]] = {}
_BRANCH_CACHE_TTL = 30.0

_NO_STORE_HINT = "No code graph yet. Run `witan code index` to build it."


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
    if repo == repo_module.detect():
        b = repo_module.store_branch()
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
    slug = repo_module.detect()
    if slug:
        client = _client_for_repo(slug, branch)
        if client is not None:
            return [client]
    return _all_clients()


def _bridge_client() -> OmnigraphClient | None:
    """Resolve the shared cross-repo bridge client, or None if not built yet."""
    store = store_module.bridge_store(cfg)
    return _client_for_path(store) if store.exists() else None


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
def code_find_definition(
    name: str, repo: str | None = None, branch: str | None = None
) -> list[dict]:
    """
    Find symbol definitions whose name or qualified name matches ``name``.

    Returns matching symbols (function, method, class, module, …) with their
    file, line range, signature, docstring, and ``repo`` of origin.

    Parameters
    ----------
    name:
        Bare name (``run``) or qualified name (``Service.run``).
    repo:
        Canonical repo URI to scope to. Omit to use the current repo, or to
        search across every indexed repo when not inside one.
    branch:
        Git branch whose indexed view to query (e.g. another agent's
        in-flight branch). Defaults to the current checkout's branch when
        querying the current repo, else the store's main view.
    """

    def _query(client: OmnigraphClient) -> list[dict]:
        rows = client.read(
            "code_read.gq", "find_by_qualified_name", {"qualified_name": name}
        )
        return rows or client.read("code_read.gq", "find_by_name", {"name": name})

    return _fan_out(_resolve_clients(repo, branch), _query)


@mcp.tool
def code_find_references(symbol_id: str) -> list[dict]:
    """
    Find symbols that reference or call ``symbol_id`` (incoming edges).

    HEURISTIC: References/Calls are derived from syntactic name resolution and
    may be incomplete or include false positives.

    Parameters
    ----------
    symbol_id:
        Full symbol id ``repo#path::QualifiedName`` (routes to its repo's store).
    """
    client = _client_for_symbol(symbol_id)
    if client is None:
        return []
    refs = client.read("code_read.gq", "referencers", {"id": symbol_id})
    callers = client.read("code_read.gq", "callers", {"id": symbol_id})
    seen = {r["slug"] for r in refs}
    return refs + [c for c in callers if c["slug"] not in seen]


@mcp.tool
def code_callers(symbol_id: str) -> list[dict]:
    """
    Find symbols that call ``symbol_id`` (incoming Calls edges).

    HEURISTIC name-resolution based; not a precise call graph.

    Parameters
    ----------
    symbol_id:
        Full symbol id ``repo#path::QualifiedName`` (routes to its repo's store).
    """
    client = _client_for_symbol(symbol_id)
    if client is None:
        return []
    return client.read("code_read.gq", "callers", {"id": symbol_id})


@mcp.tool
def code_impact(symbol_id: str, max_depth: int = 5, max_nodes: int = 200) -> dict:
    """
    Estimate change impact: the transitive set of callers of ``symbol_id``.

    Performs a breadth-first traversal over Calls edges in Python, capping at
    ``max_depth`` levels and ``max_nodes`` total. Returns the reached symbols
    and whether the traversal was truncated. HEURISTIC — see code_callers.

    Parameters
    ----------
    symbol_id:
        Full symbol id ``repo#path::QualifiedName``.
    max_depth:
        Maximum BFS depth (default 5).
    max_nodes:
        Cap on total symbols returned (default 200).
    """
    if _client_for_symbol(symbol_id) is None:
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
        "impacted": list(visited.values()),
        "truncated": truncated,
    }


@mcp.tool
def code_symbols_in_file(path: str, repo: str | None = None) -> list[dict]:
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
    slug = repo or repo_module.detect()
    if slug is None:
        return []
    client = _client_for_repo(slug)
    if client is None:
        return []
    return client.read(
        "code_read.gq", "symbols_in_file", {"file_id": _file_id(path, slug)}
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

    Use to locate a symbol when you only remember part of its name. Results carry
    their ``repo`` of origin. BM25 ranking is per-store; cross-repo fan-out
    concatenates each store's ranked results.

    Parameters
    ----------
    query:
        Search terms matched against ``qualified_name``.
    kind:
        Optional filter to a single symbol kind: ``function``, ``method``,
        ``class``, ``module``, ``variable``, ``interface``, ``type``, ``enum``,
        or ``key``. Pass e.g. ``kind="function"`` to exclude the many YAML
        ``key`` symbols when searching for code.
    repo:
        Canonical repo URI to scope to. Omit to use the current repo, or to
        search across every indexed repo when not inside one.
    branch:
        Git branch whose indexed view to query (e.g. another agent's
        in-flight branch). Defaults to the current checkout's branch when
        querying the current repo, else the store's main view.
    """

    def _query(client: OmnigraphClient) -> list[dict]:
        if kind:
            return client.read(
                "code_read.gq", "search_symbols_by_kind", {"query": query, "kind": kind}
            )
        return client.read("code_read.gq", "search_symbols", {"query": query})

    return _fan_out(_resolve_clients(repo, branch), _query)


BindingKind = Literal["env_var", "package", "service", "endpoint"]


@mcp.tool
def code_interface_providers(kind: BindingKind, key: str) -> list[dict]:
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
    """
    return _bindings_by_role(kind, key, "provider")


@mcp.tool
def code_interface_consumers(kind: BindingKind, key: str) -> list[dict]:
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
    """
    return _bindings_by_role(kind, key, "consumer")


def _bindings_by_role(kind: str, key: str, role: str) -> list[dict]:
    client = _bridge_client()
    if client is None:
        return []
    key_norm = bridge_extractors.normalize_key(kind, key)
    return client.read(
        "bridge.gq",
        "bindings_by_key_role",
        {"kind": kind, "key_norm": key_norm, "role": role},
    )


@mcp.tool
def code_cross_repo_impact(symbol_id: str) -> dict:
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
    symbol_id:
        Full symbol id ``repo#path::QualifiedName``.
    """
    empty = {"symbol_id": symbol_id, "bindings": [], "cross_repo": []}
    client = _bridge_client()
    if client is None:
        return empty

    own = client.read("bridge.gq", "bindings_for_symbol", {"symbol_id": symbol_id})
    if not own:
        return empty

    own_repo = symbol_id.split("#", 1)[0]
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
            seen.add(other["slug"])
            cross.append(other)
    return {"symbol_id": symbol_id, "bindings": own, "cross_repo": cross}


@mcp.tool
def code_interface_search(query: str, kind: BindingKind | None = None) -> list[dict]:
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
    """
    client = _bridge_client()
    if client is None:
        return []
    if kind:
        return client.read(
            "bridge.gq", "search_bindings_by_kind", {"query": query, "kind": kind}
        )
    return client.read("bridge.gq", "search_bindings", {"query": query})


@mcp.tool
def code_reindex(path: str | None = None, force: bool = False) -> dict:
    """
    Index (or re-index) the current repo, or a subpath of it.

    Incremental by default — unchanged files (matching content hash) are
    skipped. Lazily creates the per-repo store on first run. Returns a summary of
    files scanned/indexed/skipped and symbols/edges written.

    Parameters
    ----------
    path:
        Optional file or directory under the repo. Defaults to the repo root
        (or cwd if not in a git repo).
    force:
        Re-index every file regardless of content hash.
    """
    target = Path(path) if path else (repo_module.root() or Path.cwd())
    stats = indexer.index_path(target, force=force, config=cfg)
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
