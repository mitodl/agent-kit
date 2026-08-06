import asyncio
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal

from fastmcp import Context, FastMCP
from witan_core import caching
from witan_core.observability.middleware import ObservabilityMiddleware

from . import bridge_extractors
from . import config as cfg_module
from . import elicit
from . import identity as identity_module
from . import indexer
from . import ingest
from . import repo as repo_module
from . import stitch
from . import store as store_module
from . import views
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
        "code_search_symbol, code_symbols_in_file) accept `branch` — either a "
        "git branch name, or a specific writer's view of one (`act-bob/feature_x`, "
        "as listed by code_indexed_branches). A branch has one view per writer, "
        "so reads prefer this process's own and fall back to another's; naming a "
        "view reads exactly that agent's in-flight work. The default follows the "
        "current checkout's branch ONLY when querying the current detected repo; "
        "when you pass `repo` for a different repo and omit `branch`, you read "
        "that store's default (main) view — pass `branch` "
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
    # Let clients cache this server's tool list instead of re-listing it every
    # session. Scope stays private: a code graph is scoped to its repos.
    **caching.hint_kwargs(),
)

# Registered FIRST so it ends up OUTERMOST — see the note in
# witan_core.observability.middleware about why it must sit outside MRTR.
mcp.add_middleware(ObservabilityMiddleware())

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
# seconds. Tests DO have to know about it: the TTL outlives a single test, so
# it leaks across test boundaries and a later test's WITAN_REPO/branch setup
# is silently ignored inside the window. Whether that lands depends on how
# fast the preceding tests ran, which makes it a race rather than a reliable
# failure. The autouse ``_fresh_git_context`` fixture clears it between tests;
# tests that change git state *mid-test* must still call
# ``_git_context.clear()`` themselves.
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


def _client_for_ref(ref, branch: str | None = None) -> OmnigraphClient:
    key = f"{ref}|{branch or ''}"
    if key not in _clients:
        _clients[key] = ref.client(cfg, branch=branch)
    return _clients[key]


def _branch_in_store(ref, branch: str) -> bool:
    key = str(ref)
    cached = _store_branches.get(key)
    if cached is not None:
        stamp, branches = cached
        if time.monotonic() - stamp < _BRANCH_CACHE_TTL and branch in branches:
            return True
    try:
        branches = frozenset(_client_for_ref(ref).list_branches())
    except Exception:  # noqa: BLE001 — degrade to main on any listing failure
        return False
    _store_branches[key] = (time.monotonic(), branches)
    return branch in branches


def _known_branches(store) -> list[str]:
    """``store``'s branch names as of the last listing — no subprocess of its own.

    Only meaningful right after a ``_branch_in_store`` miss, which is the one
    path that reaches it: a miss re-lists and refreshes this cache (or fails,
    leaving nothing, which degrades to main). Listing again here would double
    the ``branch list`` subprocesses on every read that falls back.
    """
    cached = _store_branches.get(str(store))
    return sorted(cached[1]) if cached else []


def _resolve_branch(store, repo: str, requested: str | None) -> str | None:
    """Effective omnigraph branch for a read: None = the store's main branch.

    An explicit ``requested`` value is either a full view name (it carries a
    ``/``, so it names one specific writer's view — that is how an agent reads
    a *teammate's* in-flight work, the payoff of keeping branch views on the
    shared graph) or a git branch name, which resolves to this process's own
    view of it and then, failing that, to anyone's. Unknown either way it
    degrades to main rather than erroring.

    With no request, a query against the *current* repo follows the checkout's
    branch, again preferring this process's own view: an agent on a feature
    branch sees what it itself indexed, not a colleague's half-written state.
    """
    if requested:
        # Only an actor prefix tells a view name from a git branch — a git
        # branch may contain "/" itself (`feature/new-api`) — and a name that
        # looks like a view but isn't in this store falls through to being
        # read as a branch rather than silently degrading to main.
        if views.owner(requested) and _branch_in_store(store, requested):
            return requested
        # Same git→store mapping as indexing, so a request for branch "main"
        # in a master-default repo routes to its "_main" views rather than the
        # store's default view.
        return _view_for_branch(store, repo_module.branch_store_name(requested))
    if repo == _cached_detect():
        b = _cached_store_branch()
        if b:
            return _view_for_branch(store, b)
    return None


def _view_for_branch(store, branch: str) -> str | None:
    """This actor's view of ``branch`` in ``store``, else any, else main.

    ``branch`` is the sanitized component, already mapped by the caller (see
    ``views.views_for_branch``).

    Falling back to another writer's view is deliberate: before a developer
    has indexed a branch themselves, the closest thing to "the code on
    feature-x" is whatever view of feature-x does exist, and reading — unlike
    writing — is not the operation that needs an owner. Ties break by actor id
    so the choice is at least stable between calls.
    """
    mine = views.repo_view(branch, actor=identity_module.actor_id())
    if _branch_in_store(store, mine):
        return mine
    candidates = views.views_for_branch(_known_branches(store), branch)
    return candidates[0].name if candidates else None


def _client_for_repo(repo: str, branch: str | None = None) -> OmnigraphClient | None:
    """Client for a specific repo's store (origin scoping), or None if absent."""
    store = store_module.store_for_repo(repo, cfg)
    if not store.exists():
        return None
    return _client_for_ref(store, _resolve_branch(store, repo, branch))


def _client_for_symbol(symbol_id: str) -> OmnigraphClient | None:
    """Route a `repo#path::Name` id to the store of its repo prefix."""
    return _client_for_repo(symbol_id.split("#", 1)[0])


def _all_clients() -> list[OmnigraphClient]:
    """A client per indexed per-repo store (excludes the shared bridge store)."""
    return [_client_for_ref(ref) for ref in store_module.per_repo_stores(cfg)]


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


def _resolve_bridge_branch(store, repo: str | None) -> str | None:
    """Effective bridge branch for reads scoped to ``repo``: None = bridge main.

    Mirrors ``_resolve_branch``'s "current repo only" rule: the overlay is a
    *specific* repo's in-flight bindings on top of everyone else's main
    (docs/BRANCH_INDEXING.md § Bridge store), so it only applies
    automatically when ``repo`` is the checkout's own detected repo — an
    agent asking about some other repo's symbol while sitting elsewhere
    should not silently pick up an unrelated overlay branch.

    It mirrors the owner preference too: this actor's overlay first, then any
    writer's overlay of the same repo+branch.
    """
    if repo is None or repo != _cached_detect():
        return None
    branch = _cached_store_branch()
    if not branch:
        return None
    mine = views.bridge_view(branch, repo, actor=identity_module.actor_id())
    if _branch_in_store(store, mine):
        return mine
    candidates = views.views_for_branch(
        _known_branches(store), branch, bridge=True, repo=repo
    )
    return candidates[0].name if candidates else None


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
    return _client_for_ref(store, _resolve_bridge_branch(store, repo))


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
async def code_repo_symbols(
    repo: str | None = None,
    role: Literal["exported", "external"] | None = None,
    scheme: str | None = None,
    ctx: Context | None = None,
) -> list[dict]:
    """
    A repo's cross-repo symbol table (docs/SYMBOL_TABLE.md).

    One row per (role, symbol): ``exported`` rows are the repo's public
    contract surface — what other repos can resolve against — and ``external``
    rows are the unresolved references Stage 2 joins against other repos'
    exports. Use it to see what a repo publishes and what it expects from the
    rest of the SOA; ``code_precise_edges`` is the resolved join over the same
    table.

    Parameters
    ----------
    repo:
        Canonical repo URI. Defaults to the current detected repo.
    role:
        Filter to ``exported`` or ``external`` rows.
    scheme:
        Filter to one symbol scheme (``http``/``env``/``pkg``/``svc``).
    """
    if repo is None:
        repo = _cached_detect()
    if repo is None:
        return []
    client = _bridge_client(repo=repo)
    if client is None:
        client = await _confirm_and_reindex_bridge(ctx)
        if client is None:
            return []
    rows = client.read("bridge.gq", "repo_symbols", {"repo": repo})
    return [
        r
        for r in rows
        if (role is None or r.get("role") == role)
        and (scheme is None or r.get("scheme") == scheme)
    ]


@mcp.tool
async def code_repo_dependencies(
    kind: BindingKind | None = None,
    repo: str | None = None,
    min_precision: PrecisionTier = "heuristic",
    ctx: Context | None = None,
) -> dict:
    """
    The repo-to-repo dependency graph over every indexed repo.

    Aggregates the bridge store's interface bindings into "repo A depends on
    repo B" links (A consumes a contract B provides; for ``service`` bindings,
    the deploying repo depends on what it deploys). Returns
    ``{"repos": [...], "edges": [{consumer, provider, weight, kinds,
    contracts}]}`` — the coarse, whole-SOA view, where
    ``code_interface_providers``/``_consumers`` answer about one contract key.

    Parameters
    ----------
    kind:
        Filter to one contract kind (``env_var``/``package``/``service``/``endpoint``).
    repo:
        Keep only links touching a repo whose URI contains this substring.
    min_precision:
        ``heuristic`` (default) | ``precise`` — see server instructions.
    """
    from . import visualize

    client = _bridge_client()
    if client is None:
        client = await _confirm_and_reindex_bridge(ctx)
        if client is None:
            return {"repos": [], "edges": []}
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
    return visualize.as_payload(graph)


@mcp.tool
def code_indexed_repos() -> list[dict]:
    """
    The repositories that have a code graph indexed, and how big each is.

    Use it to check coverage before trusting a negative result — a symbol
    search returning nothing means something different when the repo in
    question was never indexed. ``files`` is None for a store that could not be
    read; ``last_indexed`` is a Unix timestamp.

    ``bytes`` and ``last_indexed`` are both null for a graph on the shared
    omnigraph-server: they describe a directory on this machine, and a client
    of a shared graph has neither the directory nor any business reporting the
    server's disk. ``files`` stays real — it is a query, not a walk.
    """

    def describe(ref) -> dict:
        size, mtime = ref.stats()
        return {
            "repo": store_module.repo_for_store(ref, cfg),
            "files": store_module.file_count(ref, cfg),
            "bytes": size,
            "last_indexed": mtime,
        }

    return store_module.map_refs(store_module.per_repo_stores(cfg), describe)


@mcp.tool
def code_indexed_branches(branch: str | None = None) -> list[dict]:
    """
    The in-flight branch views each indexed repo's store carries, and who owns each.

    A non-default git branch is indexed onto its own store view, named for the
    writer as well as the branch (docs/BRANCH_INDEXING.md), so several people
    — or an agent and its human, or one developer in two worktrees — can each
    have a view of the same branch without overwriting one another.

    Pass ``branch`` (a raw git branch name) to see every writer's view of that
    one branch: this is how you find a teammate's in-flight work. Feed a
    returned ``view`` straight back as the ``branch`` argument of
    code_find_definition / code_search_symbol / code_symbols_in_file to read
    it. Omit ``branch`` to list everything.

    Each row is ``{repo, views}``, where a view is
    ``{view, branch, actor}`` — ``view`` the name to pass back, ``branch`` the
    sanitized git branch it is a view of, ``actor`` its owner (null on a
    single-writer local store). ``views`` is null for a store that could not be
    listed.
    """
    wanted = repo_module.branch_store_name(branch) if branch else None

    def describe(ref) -> dict:
        try:
            names = _client_for_ref(ref).list_branches()
        except Exception:  # noqa: BLE001 — one bad store shouldn't abort the list
            return {"repo": store_module.repo_for_store(ref, cfg), "views": None}
        if wanted:
            found = views.views_for_branch(names, wanted)
        else:
            found = sorted(
                (views.parse_view(n) for n in names if n != "main"),
                key=lambda v: (v.branch, v.actor or ""),
            )
        return {
            "repo": store_module.repo_for_store(ref, cfg),
            "views": [
                {"view": v.name, "branch": v.branch, "actor": v.actor} for v in found
            ],
        }

    return store_module.map_refs(store_module.per_repo_stores(cfg), describe)


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
        "purged": stats.purged,
    }


# ── Store tier (machine-facing) ───────────────────────────────────
#
# The four store operations a remote indexer performs, served on its behalf
# against the cluster graphs this deployment can reach and it cannot
# (:mod:`witan_code.ingest`). Not for agents: they carry no code-graph meaning
# on their own, and `code_store_mutate` runs named mutations. Registered only
# where a remote indexer can exist — see `ingest.store_tools_enabled`.


def code_store_read(
    graph: str,
    query: str,
    name: str,
    params: dict | None = None,
    view: str | None = None,
) -> list[dict]:
    """
    Run a bundled named read query against a code graph. Machine-facing.

    Parameters
    ----------
    graph:
        Canonical repo URI, or ``bridge`` for the cross-repo bridge graph.
    query:
        Bundled query file (e.g. ``code_read.gq``).
    name:
        Named query within that file.
    params:
        Query parameters.
    view:
        Branch view to read; omit for the graph's default (main) view.
    """
    return ingest.read(graph, view, query, name, params or {})


def code_store_mutate(
    graph: str,
    query: str,
    name: str,
    params: dict | None = None,
    view: str | None = None,
) -> dict:
    """
    Run a bundled named mutation against a code graph view you own. Machine-facing.

    Refused unless the request's own identity owns ``view`` — see
    docs/BRANCH_INDEXING.md. Same arguments as ``code_store_read``.
    """
    ingest.mutate(graph, view, query, name, params or {})
    return {"graph": graph, "view": view, "query": f"{query}:{name}"}


def code_store_mutate_many(
    graph: str,
    steps: list[dict],
    view: str | None = None,
) -> dict:
    """
    Run several bundled named mutations as ONE commit. Machine-facing.

    ``steps`` is ``[{"query": file, "name": named, "params": {…}}, …]`` — the
    batch form of ``code_store_mutate``, run in order and committed once, so a
    reindex's per-file deletes cost one Lance version instead of one apiece.
    Refused unless the request's own identity owns ``view``.
    """
    return {
        "graph": graph,
        "view": view,
        "applied": ingest.mutate_many(graph, view, steps),
    }


def code_store_load(
    graph: str,
    records: list[dict],
    mode: str = "merge",
    view: str | None = None,
) -> dict:
    """
    Bulk-load node/edge records into a code graph view you own. Machine-facing.

    ``records`` are JSONL-shaped: ``{"type": Node, "data": {…}}`` for a node,
    ``{"edge": Edge, "from": key, "to": key}`` for an edge. Refused unless the
    request's own identity owns ``view``.
    """
    return {"written": ingest.load_records(graph, view, records, mode)}


def code_store_open(graph: str, view: str) -> dict:
    """
    Create a branch view (forked from main) if it does not exist. Machine-facing.

    Refused unless the request's own identity owns ``view``.
    """
    return {"view": ingest.open_view(graph, view)}


def code_store_views(graph: str) -> list[str]:
    """Every branch view on a code graph, ``main`` included. Machine-facing."""
    return ingest.views(graph)


def code_store_graphs() -> list[str]:
    """
    Canonical repo URI of every per-repo code graph served here. Machine-facing.

    The bridge graph is not listed: it is one fixed graph, addressed by name.
    """
    return ingest.graphs()


_STORE_TOOLS = (
    code_store_read,
    code_store_mutate,
    code_store_mutate_many,
    code_store_load,
    code_store_open,
    code_store_views,
    code_store_graphs,
)
_store_tools_registered = False


def register_store_tools() -> None:
    """Add the store tools to this server's surface. Idempotent.

    Called at import where a remote indexer can exist. Exposed as a function
    because whether these are registered is a property of the *process*, not
    of the module, and a test serving them in-memory has to be able to say so
    after the import that decided otherwise.
    """
    global _store_tools_registered
    if _store_tools_registered:
        return
    for tool in _STORE_TOOLS:
        mcp.tool(tool)
    _store_tools_registered = True


if ingest.store_tools_enabled():
    register_store_tools()
