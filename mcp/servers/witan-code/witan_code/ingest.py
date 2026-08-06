"""Serving a remote indexer's code-graph store operations (ADR-0005 path c).

Indexing needs a git checkout, so the indexer is ALWAYS a local process — but
the store it writes is not: on the cluster a repo's code graph is a graph on
the shared omnigraph-server, and that server is ClusterIP-only. It has no
HTTPRoute and is not getting one (DECIDED 2026-08-01): exposing the raw graph
endpoint would put a second, unmediated boundary next to the witan MCP tier
that is already exposed. So a local indexer reaches the cluster the same way
every other client does — through the MCP tier — and this module is what the
tier runs on its behalf.

WHAT MOVES SERVER-SIDE, AND WHY IT MATTERS

The client keeps every decision that needs a working tree: what to parse, what
changed, which records to write, which view to write them to. What moves here
is the pair the client was never in a position to be trusted with:

- **The identity.** ``witan_code.identity.actor_id()`` is whatever the local
  process resolved for itself. Here the actor comes from the JWT FastMCP has
  already validated, per request, exactly as witan resolves it for memory
  writes (ADR-0004). A client that lies about who it is no longer gets a
  differently-named view; it gets a refusal.
- **The authorization.** :func:`~witan_code.graph.check_writable` runs here,
  against that actor, before any mutating operation reaches the store. The
  client-side call in ``indexer.index_path`` stays, but it is now a fast-fail
  courtesy check that saves a round trip — not the authority.

The caller's *omnigraph* bearer token is resolved here too, from the same
provisioned ``{actor_id: token}`` map witan uses, so the write lands on the
data tier as the caller rather than as the service account. Cedar sees the
real principal.

WHY A STORE SURFACE AND NOT AN "INGEST" ONE

The obvious shape is one bulk-ingest tool taking a batch of records. It is not
enough: a repo index is a read (existing file hashes), a per-file delete for
everything that changed, a bulk load, and then the whole thing again against
the bridge graph with its own reads and purges (:mod:`witan_code.bridge`).
Modelling each of those as its own tool would move indexing *policy* onto the
server, where it would have to be kept in step with a client that can be a
release behind. So the tools mirror the four store operations the write path
actually performs, and :class:`witan_code.remote.store.RemoteStoreClient`
stands in for an ``OmnigraphClient`` on the client side. Nothing in
``indexer``/``bridge`` changes, and the server stays a mediated transport.

Mediated, not arbitrary: ``query`` names a file bundled in this package's
``queries/`` directory and ``name`` a query inside it, so the surface is the
same named queries the in-process path can run, on a graph resolved from a
repo URI rather than from anything the caller sends.

The cost is round trips. That is the same subprocess-per-call shape the local
path already has, plus a network hop; the CI indexer (the one writer that does
full-repo runs routinely) runs in-cluster and keeps the direct ``code_server``
path, so what crosses this boundary is developer-branch reindexes of what
changed. See tk-spike-subprocess-per-call-overhead-for-remote-om-d6ceac.

Which is why the tools are not strictly one-per-store-operation:
:func:`mutate_many` mirrors ``OmnigraphClient.change_many``, because a reindex
emits two deletes per changed file and one call apiece made the round trips —
and the Lance versions — scale with the repo. The splice stays here, where
``queries_dir`` is, so the client still sends only named queries and params.
"""

from __future__ import annotations

import os
import re
from dataclasses import replace

from witan_core.identity import ActorTokenResolver, derive_actor_id

from . import config as cfg_module
from . import identity as identity_module
from . import store as store_module
from .graph import OmnigraphClient, SharedGraphWriteRefused, check_writable

__all__ = [
    "BRIDGE",
    "IngestRefused",
    "STORE_TOOLS_ENV_VAR",
    "graphs",
    "load_records",
    "mutate",
    "open_view",
    "read",
    "request_actor",
    "store_tools_enabled",
    "views",
]

BRIDGE = cfg_module.BRIDGE_GRAPH_ID
"""``graph`` value selecting the shared cross-repo bridge graph.

Every other graph is named by the repo it holds; the bridge holds every repo's
bindings and belongs to none, so it travels as its own cluster graph id. No
canonical repo URI can collide with it — they all carry a scheme.
"""

STORE_TOOLS_ENV_VAR = "WITAN_CODE_STORE_TOOLS"
"""Force the store tools on (``1``) or off (``0``), overriding the default.

They are registered by default only where they are useful and safe: a
deployment, which is what a remote indexer talks to. A local stdio server
writes its own stores directly, so registering them there would add four
machine-facing tools to every agent's tool list — one of which runs named
mutations — to serve a caller that cannot exist.
"""

# Same discriminator witan's server uses for "am I a deployment" (see
# witan.config.load_identity_config): OIDC configured means requests arrive as
# validated JWTs, which is the only mode in which these tools can resolve a
# caller at all.
_DEPLOYMENT_ENV_VAR = "WITAN_OIDC_ISSUER"

# A query file bundled with this package. Anchored and separator-free: `query`
# arrives from a client, and this is the one argument that names a file.
_QUERY_FILE_RE = re.compile(r"^[a-z_]+\.gq$")


class IngestRefused(RuntimeError):
    """A store operation was refused before it reached the graph.

    Unauthenticated, unauthorized, or addressed at something this server does
    not serve. Distinct from a store error: nothing was written.
    """


def store_tools_enabled() -> bool:
    """Whether to register the store tools in this process."""
    override = os.environ.get(STORE_TOOLS_ENV_VAR)
    if override is not None:
        return override.strip().lower() in ("1", "true", "yes", "on")
    return bool(os.environ.get(_DEPLOYMENT_ENV_VAR))


def _config() -> cfg_module.Config:
    """This server's own view of where the code graphs are.

    A seam, and the only one in this module: an in-process test drives a
    client and a server that share one environment, and the two need different
    answers to exactly this question — the client's says "reach the graphs
    through the MCP tier", and a server that agreed would proxy to itself.
    """
    return cfg_module.load()


def _actor_tokens() -> ActorTokenResolver | None:
    """Resolver over the deployment's provisioned ``{actor_id: token}`` map.

    Rebuilt per call rather than at import: witan's server resolves the same
    file into its own long-lived resolver, and a second process-lifetime cache
    of the same map here would have to be invalidated in step with it. The
    resolver's own reload check is a ``stat()``, so the only cost of a fresh
    one is re-reading a small JSON file per store call — dwarfed by the
    omnigraph subprocess the call is about to spawn.
    """
    path = os.environ.get("WITAN_ACTOR_TOKENS_FILE")
    return ActorTokenResolver(path) if path else None


def request_actor() -> str | None:
    """The actor id for the current MCP request, or ``None`` under local stdio.

    A deployment reaches its tool handlers only through FastMCP's JWT
    verifier, so a validated token is present for every tool call; its absence
    means this is not a tool request (an admin CLI command inside the
    container) and there is no caller to identify.

    ``None`` also covers the local stdio server, where the process's own
    identity is the only one there is.
    """
    if not os.environ.get(_DEPLOYMENT_ENV_VAR):
        return identity_module.actor_id()
    # Imported at call time: fastmcp's request-context machinery is only
    # meaningful inside a request, and this module is imported by the CLI too.
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    if token is None:
        return identity_module.actor_id()
    return derive_actor_id(token.claims.get("sub", ""))


def _client(
    graph: str,
    view: str | None,
    cfg: cfg_module.Config,
    actor: str | None,
) -> OmnigraphClient:
    """An omnigraph client for ``graph``'s ``view``, authenticated as ``actor``.

    The store address comes from this server's own configuration — the
    caller names a repo, never a URL — and the bearer token from the actor
    the JWT resolved to, so the data tier attributes the write to the person
    who asked for it rather than to the service.

    A deployment with no token provisioned for the actor refuses here: falling
    back to the server's configured ``code_token`` would write the caller's
    records under the service identity, which is precisely the attribution
    this layer exists to prevent.

    Resolution goes through ``ensure_store``, so a graph the cluster does not
    declare is a clear refusal on the first call rather than one store error
    per record — and a dev deployment over local stores creates the store it
    is being asked to serve, exactly as an in-process index would.
    """
    ref = (
        store_module.ensure_bridge_store(cfg)
        if graph == BRIDGE
        else store_module.ensure_store(graph, cfg)
    )
    resolver = _actor_tokens()
    if resolver is not None:
        if actor is None:
            raise IngestRefused(
                "This deployment resolves the omnigraph identity from the "
                "caller's token, and this request carries none."
            )
        try:
            ref = replace(ref, token=resolver.resolve(actor))
        except LookupError as exc:
            raise IngestRefused(str(exc)) from exc
    return ref.client(cfg, branch=view)


def _query_path(cfg: cfg_module.Config, query: str) -> str:
    if not _QUERY_FILE_RE.match(query):
        raise IngestRefused(
            f"{query!r} does not name a query file bundled with witan-code "
            "(expected e.g. 'code_read.gq')."
        )
    if not (cfg.queries_dir / query).is_file():
        raise IngestRefused(f"This server has no query file named {query!r}.")
    return query


def _authorize(
    graph: str,
    view: str | None,
    cfg: cfg_module.Config,
    actor: str | None,
) -> None:
    """Refuse a write the request's actor does not own. See :func:`check_writable`.

    ``is_remote=True`` unconditionally, and not because of how this server's
    store happens to be addressed: a write arriving here is somebody else's,
    which is the only reason the request exists. A deployment whose own graphs
    were local directories (a dev instance) would otherwise apply the
    single-writer rule — "one machine, one user" — to writers on several.

    A consequence worth stating: ``view is None`` targets the shared default
    view, whose one writer is the CI indexer, and CI writes in-cluster over
    the direct transport. So no request through this boundary can claim it
    unless the deployment itself is declared ``index_role = ci``.

    ``SharedGraphWriteRefused`` becomes :class:`IngestRefused` so a caller
    sees one refusal type whichever check rejected it — the message, which
    already explains which view is owned by whom, is what carries the detail.
    """
    try:
        check_writable(is_remote=True, branch=view, cfg=cfg, slug=graph, actor=actor)
    except SharedGraphWriteRefused as exc:
        raise IngestRefused(str(exc)) from exc


# ── Operations ────────────────────────────────────────────────────────────────


def read(
    graph: str, view: str | None, query: str, name: str, params: dict
) -> list[dict]:
    """Run named read query ``name`` from ``query`` against ``graph``'s ``view``.

    Unauthorized only in the sense that every view of a shared graph is
    readable by everyone — that is the decision branch views live on the
    shared graph for. What a reader may see is the data tier's business:
    the call carries the caller's own bearer token, so Cedar applies to it.
    """
    cfg = _config()
    client = _client(graph, view, cfg, request_actor())
    return client.read(_query_path(cfg, query), name, params)


def mutate(graph: str, view: str | None, query: str, name: str, params: dict) -> None:
    """Run named mutation ``name`` from ``query`` against ``graph``'s ``view``."""
    cfg = _config()
    actor = request_actor()
    client = _client(graph, view, cfg, actor)
    _authorize(graph, view, cfg, actor)
    client.change(_query_path(cfg, query), name, params)


def mutate_many(graph: str, view: str | None, steps: list[dict]) -> int:
    """Run several named mutations against ``graph``'s ``view`` as ONE commit.

    ``steps`` is ``[{"query": file, "name": named, "params": {…}}, …]`` — the
    wire form of the triples :func:`mutate` takes one at a time. They are handed
    to the server-side client's ``change_many``, which splices them into a
    single multi-statement mutation, so N rows cost one Lance version instead of
    N. Returns the number of steps applied.

    WHY THE STEPS ARRIVE AS PARAMS AND NOT AS A COMPOSED BODY. The splice needs
    each named query's source, which lives in this server's ``queries_dir``; the
    client has no business composing GQ, and a tool that accepted an inline body
    would let a caller send arbitrary GQ through a surface Cedar scopes by named
    query. So the client sends only what it already sends today — file, name,
    params — and the composition stays here.

    ORDER IS PRESERVED AND SIGNIFICANT: an edge statement may reference a node
    an earlier step inserted. Every step is validated before any of them runs,
    so a bad file name in the middle of a batch refuses the whole batch rather
    than committing its prefix.

    NOT for a compare-and-swap, for the same reason ``change_many`` is not: the
    batch commits or fails whole, so a conflict cannot be attributed to a step.
    """
    cfg = _config()
    actor = request_actor()
    client = _client(graph, view, cfg, actor)
    _authorize(graph, view, cfg, actor)
    triples = [
        (
            _query_path(cfg, _step_field(step, "query")),
            _step_field(step, "name"),
            _step_params(step),
        )
        for step in steps
    ]
    client.change_many(triples)
    return len(triples)


# The tool signature is `steps: list[dict]`, and FastMCP validates that much —
# a non-dict step is refused at the boundary and never reaches here. What the
# schema does NOT constrain is anything INSIDE a step: `dict` is `dict[str, Any]`,
# so `{"query": 7}`, a missing `query`, and `{"params": "oops"}` all arrive
# intact. Hence these two guards, and only these two.


def _step_field(step: dict, field: str) -> str:
    value = step.get(field)
    if not isinstance(value, str) or not value:
        raise IngestRefused(f"Every step needs a non-empty {field!r}; got {value!r}.")
    return value


def _step_params(step: dict) -> dict:
    """A step's params, refusing anything that is not a mapping.

    Omitted and null both mean "no params". Anything else has to be a dict: the
    value is handed to ``change_many`` and ends up as the ``--params`` JSON of a
    composed mutation, so a string or a list would surface as an opaque omnigraph
    CLI failure several layers below the caller that sent it.
    """
    params = step.get("params")
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise IngestRefused(
            f"A step's 'params' must be an object; got {type(params).__name__}."
        )
    return params


def load_records(
    graph: str,
    view: str | None,
    records: list[dict],
    mode: str = "merge",
) -> int:
    """Bulk-load ``records`` into ``graph``'s ``view``. Returns the count written.

    The volume path: one call carries every node and edge record a file batch
    produced, exactly as the local ``omnigraph load`` does.
    """
    cfg = _config()
    actor = request_actor()
    client = _client(graph, view, cfg, actor)
    _authorize(graph, view, cfg, actor)
    client.load(records, mode=mode)
    return len(records)


def open_view(graph: str, view: str) -> str:
    """Create ``view`` on ``graph`` (forked from main) if it does not exist.

    Reads never fork a branch — only ``load --from`` does — so a client's very
    first read of a new view has to be preceded by this, exactly as
    ``OmnigraphClient.ensure_branch`` does in-process.
    """
    cfg = _config()
    actor = request_actor()
    client = _client(graph, view, cfg, actor)
    _authorize(graph, view, cfg, actor)
    client.ensure_branch()
    return view


def views(graph: str) -> list[str]:
    """Every branch view on ``graph``, ``main`` included."""
    cfg = _config()
    return _client(graph, None, cfg, request_actor()).list_branches()


def graphs() -> list[str]:
    """The repo URI of every per-repo code graph this server holds.

    What a client asks instead of listing graph ids and inverting them: the
    server's own resolution is the authority for which repo a graph holds, and
    :func:`config.graph_id` does not invert. The bridge graph is not in the
    list — it is one fixed graph, addressed as :data:`BRIDGE`.
    """
    cfg = _config()
    return store_module.map_refs(
        store_module.per_repo_stores(cfg),
        lambda ref: store_module.repo_for_store(ref, cfg),
    )
