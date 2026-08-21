import contextvars
import functools
import hashlib
import importlib.metadata
import inspect
import json
import math
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import anyio.to_thread
from fastmcp import Context, FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token
from starlette.requests import Request
from starlette.responses import JSONResponse
from witan_core import caching, chunking, normalise, now_iso
from witan_core import omnigraph_install
from witan_core.observability import get_logger
from witan_core.observability.middleware import ObservabilityMiddleware
from witan_core.omnigraph import (
    acquire_store_flock,
    release_store_flock,
    schema_apply,
    schema_apply_if_changed,
    store_cli_args,
)

from . import config as cfg_module
from . import elicit, readiness, scan, session_state
from . import repo as repo_module
from .graph import (
    OmnigraphClient,
    OmnigraphConflict,
    StoreUnavailable,
    _is_storage_version_mismatch,
)
from .identity import ActorTokenResolver, derive_actor_id

# ── Startup ───────────────────────────────────────────────────────

logger = get_logger("witan.server")

# Reported by `/health`, so "which image is actually serving?" is answerable
# with a curl instead of an exec into the pod — the question every rollout of
# this workload asks first. Resolved once at import: the answer cannot change
# while the process lives, and a probe endpoint should not do work per request.
# Falls back rather than raising, because a missing distribution must not be
# what makes a healthy process report itself unhealthy.
# The DISTRIBUTION name, which is `witan-council` — the import package is
# `witan`, and asking for that returns "unknown" without raising, so getting
# this wrong is silent.
try:
    _VERSION = importlib.metadata.version("witan-council")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover - installed in situ
    _VERSION = "unknown"

_SCHEMA_FILE = Path(__file__).parent.parent / "schema" / "schema.pg"


def _ensure_graph(graph_uri: str) -> None:
    """Initialise the local graph, and keep its schema current.

    No-op for remote (http/s3) URIs — those are assumed to be managed
    externally. A deployment's schema is applied by provisioning, and against a
    server ``schema apply`` takes the ``--server <url> --graph <id>`` form
    rather than the local positional one, so there is nothing correct to do here.

    Local stores are created and schema-applied automatically so `witan serve`
    and `witan <cmd>` work on a fresh machine without a separate install step.
    An EXISTING store is re-applied when ``schema.pg`` has changed since the
    last apply, which is what picks up additive changes (new node types, new
    fields) after a version bump — previously that only happened if the user
    knew to run `witan migrate`, the `apply_schema` admin tool, or `install.sh`,
    and until they did, queries against the newer schema erred or silently
    returned nothing.

    The re-apply deliberately cannot raise: this runs at import time
    (``_ensure_graph(cfg.graph_uri)`` below), so a failure would take down
    `witan serve` at startup. A failed apply leaves the stamp unwritten and is
    retried next time. That includes locating the binary — ``_find_binary``
    raises when omnigraph is not on PATH, so for an EXISTING store a missing
    binary degrades to "skip the re-apply" rather than propagating. (Import
    still fails a few lines below, where the ``OmnigraphClient`` constructor
    resolves the same binary — so this changes which error a user sees, not
    whether witan works without omnigraph installed. It keeps this function's
    contract honest, and keeps the other caller, ``merge_store``, from
    inheriting a raise it does not expect.)

    Creation keeps ``check=True``, and lets a missing binary raise — a store
    that does not exist yet has nothing to degrade to.
    """
    if graph_uri.startswith(("http://", "https://", "s3://")):
        return
    store = Path(graph_uri).expanduser()
    if store.exists():
        try:
            binary = OmnigraphClient._find_binary()
        except RuntimeError:
            return
        schema_apply_if_changed(binary, _SCHEMA_FILE, store)
        return
    binary = OmnigraphClient._find_binary()
    store.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [binary, "init", "--schema", str(_SCHEMA_FILE), str(store)],
        check=True,
        capture_output=True,
        text=True,
    )
    schema_apply(binary, _SCHEMA_FILE, store)


cfg = cfg_module.load()
rank_cfg = cfg_module.load_rank_config()
scan_cfg = cfg_module.load_scan_config()
identity_cfg = cfg_module.load_identity_config()
_ensure_graph(cfg.graph_uri)
_write_guard = scan.write_guard_from_config(scan_cfg)
_default_client = OmnigraphClient(
    cfg.graph_uri,
    cfg.queries_dir,
    cfg.graph_token,
    guard=_write_guard,
    graph_id=cfg.graph_name,
)

# Per-user actor/token mapping for the deployed streamable-http service (ADR
# 0004). None in local stdio use, where identity_cfg.oidc_issuer is unset.
actor_token_resolver = (
    ActorTokenResolver(identity_cfg.actor_tokens_file)
    if identity_cfg.actor_tokens_file
    else None
)
_jwt_verifier = (
    JWTVerifier(
        jwks_uri=f"{identity_cfg.oidc_issuer.rstrip('/')}/protocol/openid-connect/certs",
        issuer=identity_cfg.oidc_issuer,
        audience=identity_cfg.oidc_audience,
    )
    if identity_cfg.oidc_issuer
    else None
)

_actor_clients: dict[str, OmnigraphClient] = {}
_actor_clients_lock = threading.Lock()


def _resolve_client() -> OmnigraphClient:
    """Return the ``OmnigraphClient`` for the current MCP request's actor.

    Local stdio use (``identity_cfg.oidc_issuer`` unset) always returns the
    single process-lifetime ``_default_client`` — byte-identical to
    pre-ADR-0004 behavior.

    In deployed streamable-http mode, a validated JWT is required to reach
    any tool handler (FastMCP itself rejects unauthenticated requests via
    ``_jwt_verifier``), so ``get_access_token()`` returning ``None`` here
    means this call is *not* a tool request — e.g. an admin/migration CLI
    command (``apply-schema``, ``migrate-topics``) invoked directly inside
    the deployed container. Those fall back to the shared default client
    rather than erroring, since they have no per-user identity to isolate.

    Otherwise, the JWT's ``sub`` claim is mapped to an actor id and its
    pre-provisioned omnigraph bearer token is looked up, and a client for
    that actor is constructed once and cached — ``OmnigraphClient`` shells
    out to a subprocess per call, so reusing one across a user's requests
    (rather than rebuilding it every call) is worth the cache.
    """
    if not identity_cfg.oidc_issuer:
        return _default_client
    token = get_access_token()
    if token is None:
        return _default_client
    actor_id = derive_actor_id(token.claims.get("sub", ""))
    if actor_id in _actor_clients:
        return _actor_clients[actor_id]
    with _actor_clients_lock:
        if actor_id not in _actor_clients:
            assert actor_token_resolver is not None  # implied by oidc_issuer being set
            bearer = actor_token_resolver.resolve(actor_id)
            _actor_clients[actor_id] = OmnigraphClient(
                cfg.graph_uri,
                cfg.queries_dir,
                bearer,
                guard=_write_guard,
                graph_id=cfg.graph_name,
            )
    return _actor_clients[actor_id]


def _current_author() -> str:
    """The identity to attribute this request's writes to.

    Sibling of :func:`_resolve_client`, which routes a write to the caller's
    omnigraph client but never touches the ``author`` *value*. Without this,
    every node a deployment writes carries the server container's configured
    author, so ``workflow_trace_list(author=…)`` filters on a field with one
    value deployment-wide and mined traces carry no usable provenance.

    Local stdio use keeps ``cfg.author`` (``WITAN_AUTHOR`` / git config /
    ``$USER``), which is already the right answer there. Same discriminator as
    ``_resolve_client`` / ``_is_local_stdio``; ``get_access_token()`` returning
    ``None`` under a deployment means an admin/migration CLI call rather than a
    tool request, which likewise has no caller identity to attribute.

    Prefers ``preferred_username`` so the value stays human-readable for author
    filters and the ranking author-trust config, falling back to ``email`` and
    then to the derived ``act-<sub>`` — the same id the token-mapping layer
    uses, so attribution degrades to opaque-but-correct rather than to the
    wrong user.
    """
    if _is_local_stdio():
        return cfg.author
    token = get_access_token()
    if token is None:
        return cfg.author
    claims = token.claims
    for claim in ("preferred_username", "email"):
        value = claims.get(claim)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return derive_actor_id(claims.get("sub", ""))


class _ActorScopedClient:
    """Proxy that delegates every attribute access to ``_resolve_client()``.

    Bound to the module-level name ``client`` so none of the ~30 tool
    handlers (and the helpers they call) need to change: each ``client.foo``
    reference already re-resolves to the current request's actor client on
    every access, rather than closing over one process-lifetime client.
    """

    def __getattr__(self, name: str):
        return getattr(_resolve_client(), name)


client = _ActorScopedClient()

# One named mutation and its parameters: ``(query_file, query_name, params)``,
# the triple both ``client.change`` and ``client.change_many`` take. Named so a
# helper can hand its caller a mutation to commit *with* the caller's own —
# several rows in one Lance commit instead of one commit each.
_Step = tuple[str, str, dict]


def apply_schema() -> dict:
    """Apply the bundled ``schema.pg`` to the configured store (idempotent).

    Reconciles an EXISTING store with the current schema. ``_ensure_graph`` now
    does this automatically when ``schema.pg``'s mtime changes, so this is the
    explicit escape hatch for the cases the mtime stamp cannot see: a store
    whose stamp is current but whose schema is not (a restored backup, a
    hand-edited stamp), or a re-apply forced without touching the file.

    Routes through ``OmnigraphClient`` so it holds the same per-store write lock +
    retry/repair as any other write. Returns ``{"store", "output"}``; raises
    ``RuntimeError`` on a failed apply.
    """
    output = client.apply_schema(_SCHEMA_FILE)
    return {"store": client.graph_uri, "output": output.strip()}


def _topic_schema_present() -> bool:
    """True if the store knows the ``Topic`` type (i.e. schema has been applied).

    Probes with a harmless lookup; a store on the pre-graph schema raises an
    ``unknown type`` error, which lets ``migrate topics`` fail fast with guidance
    instead of an opaque engine error mid-backfill.
    """
    try:
        client.read("read.gq", "get_topic", {"slug": "tp-__schema_probe__"})
    except RuntimeError as exc:
        # The pre-graph schema raises exactly: "unknown node type `Topic`".
        # Match that specifically so connection/other errors propagate.
        msg = str(exc).lower()
        if "unknown node type" in msg and "topic" in msg:
            return False
        raise
    return True


mcp = FastMCP(
    "witan",
    auth=_jwt_verifier,
    instructions=(
        "Team-wide, shared, persistent memory and work-coordination graph. PREFER "
        "storing durable, shareable knowledge here — project facts, patterns, "
        "lessons, decisions, and hand-off context — over your private "
        "built-in/session memory, so other agents, future sessions, and teammates "
        "can find it. Record what you learn with memory_store. Also tracks "
        "workflow projects, sessions, and tasks.\n\n"
        "Loading context: call recall(query=…) — it composes every memory read "
        "(BM25 + graph expansion + superseded-pruning + re-ranking) and degrades "
        "to a plain search when the graph has no edges. Seed it with symbol_id, "
        "task, or topic instead of/along with query. Reach for a narrower read "
        "only for a specific need:\n"
        '  recall           — default; contextual load / "what do we know about X"\n'
        "  memory_get       — one memory by slug\n"
        "  memory_list      — browse all of one kind (no query); optional language\n"
        "  memory_search    — plain BM25, no graph expansion\n"
        "  memory_neighbors — a known memory's neighbours, by edge kind\n"
        "  topic_get        — everything tagged to a topic (cross-repo)\n"
        "  memory_for_contract — memories + code for a contract key\n"
        "  symbol_context   — memories/tasks for a code symbol\n"
        "  memory_symbols   — the code symbols a memory concerns\n"
        "  workflow_project_memories — memories a project produced\n\n"
        "Repo scoping: every tool auto-detects the current repo from .git/config. "
        "Pass repo (a canonical URI like https://github.com/mitodl/ol-django) to "
        'override it, or repo="" to operate across all repos. When a list tool '
        "detects no repo and none is passed, it returns slim records (slug, kind, "
        "title, tags — no content); memory_get a slug for the full memory.\n\n"
        "Changing memory — pick by what is actually wrong:\n"
        "  a field is wrong (wrong repo, typo'd title) → memory_update(slug, …); "
        "only the fields you pass change\n"
        "  the knowledge changed → memory_store the new one, then "
        'memory_link(from_slug=<new>, to_slug=<old>, kind="supersedes") — the old '
        "one is hidden from default reads but kept (include_superseded=True to "
        "see it)\n"
        "  it should never have existed (accidental duplicate, test write) → "
        "memory_delete(slug, confirm=True); author-only, hard delete\n"
        "  it contains a secret → rotate the credential. memory_delete removes it "
        "from the current graph but not from history, and no tool can erase "
        "that.\n\n"
        "Naming: the task_* tools track work items (task_create, task_claim, "
        "task_ready, …) and have nothing to do with MCP's own tasks/* extension "
        "for long-running calls. A task_* slug is a unit of work someone is "
        "assigned; an MCP task id is a handle on a call still executing.\n\n"
        "Errors: a lookup that finds nothing returns null/empty, never raises; an "
        "invalid-but-well-formed mutation (self-link, self-block, claim "
        "contention) returns a status object with a reason; only malformed input "
        "raises."
    ),
    # Let clients cache this server's ~37-tool list instead of re-listing it
    # every session. Scope stays private: memory reads are per-actor.
    **caching.hint_kwargs(),
)

# Registered FIRST so it ends up OUTERMOST — fastmcp composes its chain with
# `reversed(self.middleware)`. It has to sit outside MRTR below, which turns a
# raised InputRequired into a *successful* result: from inside, every ordinary
# elicitation would be recorded as a failed tool call.
mcp.add_middleware(ObservabilityMiddleware())

# Carries `elicit.confirm`/`elicit.text` asks over MCP 2026-07-28, which has no
# server→client back-channel to run them on. Inert on the handshake eras.
mcp.add_middleware(elicit.MRTRElicitationMiddleware())


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    """Liveness/readiness for a deployed witan. Deliberately shallow.

    ★ THIS MUST NEVER TOUCH THE GRAPH, and that is the whole design. The
    handler answers from process state alone: if the event loop can run this
    coroutine, the process is alive, and that is the only question a kubelet
    probe is entitled to ask.

    A probe that checks the data tier looks more truthful and is strictly
    worse. It is the exact failure that took the deployed service down on
    2026-08-12: ToolHive's proxy `/health` synchronously pinged its backend
    (upstream `pkg/healthcheck/healthcheck.go:CheckHealth`), a burst of
    concurrent writes saturated that backend, the ping stopped answering, and
    the kubelet's 5s liveness probe killed a container that was working
    perfectly — turning a slow write queue into ~60s of total outage for
    readers too. Depth converts backend SLOWNESS into frontend DEATH, and
    under load it fires precisely when killing the pod is most harmful.

    witan cannot serve a tool call without omnigraph, so a graph outage is
    real — but the signal for it belongs in alerting on the spans and metrics
    witan already emits, where it degrades a dashboard instead of a pod.

    Unauthenticated, unlike every MCP tool: FastMCP applies `auth=` to the
    protocol endpoint only, so a `custom_route` is reachable without a bearer
    token — which is required here, since the kubelet carries none. Verified
    against fastmcp 4.0.0b2 rather than assumed: a JWT-guarded server answers
    GET /health 200 and POST /mcp 401. Nothing here is worth authenticating;
    the response carries no graph data and no per-actor state.
    """
    return JSONResponse({"status": "ok", "service": "witan", "version": _VERSION})


async def _offload(fn, /, *args, **kwargs):
    """Run blocking store work off the event loop.

    ★ EVERY OMNIGRAPH CALL SHELLS OUT TO A SUBPROCESS, AND AN ``async def`` TOOL
    RUNS ON THE EVENT LOOP. FastMCP dispatches SYNC tool functions through a
    threadpool, so they can block all they like; the handful of tools that are
    ``async`` — only so they can ``await`` an elicitation helper — run on the
    loop itself. A synchronous ``client.read``/``change``/``change_many`` inside
    one of those stops the loop dead for the length of the subprocess, and
    nothing else gets scheduled: not another request, not ``/health``.

    ★ THAT IS NOT A CAPACITY PROBLEM, WHICH IS WHY IT LOOKED SO STRANGE.
    Measured in QA on 2026-08-17 at 16 concurrent writers: the pod used **0.011
    cores** — 1.1% of one CPU, with no limit set — while ``/health`` failed
    probes at 5s AND at 10s, readiness ejected the only replica, and APISIX
    answered every caller with 503 while all 16 writes committed. A loop that is
    merely BUSY burns CPU; a loop that cannot schedule a trivial coroutine for
    ten seconds at 1% CPU is BLOCKED, waiting on I/O it never yielded from.

    Wrapping each store call rather than the whole handler is deliberate: the
    loop is released between calls too, so a tool making several of them lets
    other coroutines interleave instead of holding a thread for the whole body.

    ``functools.partial`` because ``anyio.to_thread.run_sync`` takes positional
    arguments only — keywords are what most of these calls use.

    ★ THE CONTEXT DANCE IS NOT CEREMONY — WITHOUT IT THIS SILENTLY LOSES
    REDACTION NOTICES. ``witan.scan.notice`` records them in a ``ContextVar``,
    which is COPIED into a worker thread rather than shared, so a ``.set()``
    made in there is invisible to the caller. ``scan.notice``'s own comment
    states the invariant this breaks: "``record`` and ``annotate`` must run in
    the SAME context". The first version of this helper ignored that, and three
    tools began rewriting content while telling the caller nothing — the exact
    data-loss path that module exists to close.

    So the call runs inside a ``Context`` this function owns. Mutations under
    ``Context.run`` persist on that object, so ``notice.adopt`` can merge them
    back into the caller's context afterwards. Caught by the existing
    ``test_scan_notice`` tests, which is why they are worth having.
    """
    ctx = contextvars.copy_context()
    try:
        return await anyio.to_thread.run_sync(
            functools.partial(ctx.run, functools.partial(fn, *args, **kwargs))
        )
    finally:
        # ★ IN A `finally` BECAUSE THE GUARD RECORDS BEFORE THE WRITE IS ISSUED.
        # `scan.enforce` notes the redaction and then hands the rewritten params
        # to the mutation, so a call that redacts and *then* raises — an
        # `OmnigraphConflict` from `_update_task(surface_conflict=True)`, say —
        # has already produced a notice. Adopting only on success would discard
        # it, which is the silent-rewrite failure this module exists to prevent,
        # just narrowed to the error path.
        scan.notice.adopt(ctx)


def _tool(fn):
    """Register an MCP tool, reporting any content its writes rewrote.

    ★ EVERY TOOL, AND AT THE OUTERMOST BOUNDARY — both halves are load-bearing,
    and the first attempt at this got both wrong by calling ``scan.annotate``
    inside the write helpers (``_store_memory``, ``_update_memory``,
    ``_update_task``). Reading a notice CONSUMES it, so a helper that reports
    early both misses later writes and destroys the evidence for its caller:

    * ``memory_update`` writes its tag→Topic batch AFTER ``_update_memory``
      returns, so a redacted Topic name was recorded past the point anything
      looked, and the already-built result went back clean.
    * ``workflow_trace_mine`` keeps only ``_store_memory(...)["slug"]`` and
      ``task_claim``/``task_release`` build their own responses from
      ``_update_task``, so the annotated dict — and with it the only record of
      the redaction — was dropped on the floor.
    * ``memory_link`` and ``migrate_topics`` issue guarded ``insert_topic``
      mutations and were never wired up at all.

    Wrapping the tool function is what makes those unrepresentable rather than
    merely fixed: the wrapper cannot run before the tool's last write, and there
    is no caller between it and the client to discard what it returns. A new
    write tool is covered by construction — which the enumerate-the-write-paths
    approach could never promise, and had already failed to deliver four times.

    Read tools pay one ``take_redactions()`` on an empty contextvar.

    ``functools.wraps`` copies ``__wrapped__``, so ``inspect.signature`` — and
    therefore FastMCP's schema generation — still sees the real parameters.
    """
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            return scan.annotate(await fn(*args, **kwargs))
    else:

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            return scan.annotate(fn(*args, **kwargs))

    return mcp.tool(wrapper)


# ── Helpers ───────────────────────────────────────────────────────

MemoryKind = Literal["pattern", "project_fact", "lesson", "agent_context"]

MemoryLinkKind = Literal[
    "supersedes", "refines", "applies_to", "contradicts", "related_to", "tagged"
]

TopicKind = Literal["topic", "contract", "symbol", "entity"]

# Edge kind → mutation query that writes it. ``tagged`` (Memory → Topic) is
# handled separately in memory_link since its target is a Topic, not a Memory.
_MEMORY_LINK_MUTATIONS = {
    "supersedes": "link_supersedes",
    "refines": "link_refines",
    "applies_to": "link_applies_to",
    "contradicts": "link_contradicts",
    "related_to": "link_related_to",
}

# Edge kind → read queries whose union gives a memory's neighbours along it.
# Symmetric kinds (contradicts, related_to) are stored one direction and unioned
# from both at read time.
_MEMORY_NEIGHBOR_QUERIES = {
    "supersedes": ["supersedes_targets"],
    "refines": ["refines_targets"],
    "applies_to": ["applies_to_targets"],
    "contradicts": ["contradicts_out", "contradicts_in"],
    "related_to": ["related_out", "related_in"],
}

_KIND_PREFIX = {
    "pattern": "pat",
    "project_fact": "pf",
    "lesson": "les",
    "agent_context": "ctx",
    "workflow_project": "wp",
    "workflow_session": "ws",
    "workflow_trace": "wt",
    "task": "tk",
}


# Advisory-claim lease lives in ``readiness`` (shared with the context hook so
# the injected "Ready Tasks" list and ``task_ready`` agree). Re-exported under
# the historical names for the in-module call sites.
_CLAIM_LEASE_SECONDS = readiness.CLAIM_LEASE_SECONDS
_lease_expired = readiness.lease_expired

# A task can be held (in_progress, lease not expired) with no assignee on
# record — e.g. moved to in_progress via task_update rather than task_claim.
# ``held_by: None`` in that case is ambiguous to API consumers (does it mean
# "not held" or "held, holder unknown"?), so every held_by response field uses
# this stable placeholder instead of the raw (possibly-None) holder.
_UNKNOWN_HOLDER = "unknown (no assignee on record)"

# A holder is ``"<identity>#<session>"`` — see ``_claim_holder``. Matched
# conservatively (the charset of a session id, anchored at the end) so an
# identity that happens to contain a '#' is not mistaken for a qualified one.
#
# '#' rather than the more obvious "identity [session]": holder strings are
# printed straight into `rich` consoles by the task CLI, and rich reads
# ``[aaaaaaaa]`` as a style tag and *swallows* it. That silently rendered every
# session's holder as the bare identity again — reintroducing the exact
# indistinguishability this qualifier exists to remove, at the one place a human
# reads it. A delimiter that is not markup in any of our output paths avoids
# having to remember to escape it at each one.
_SESSION_SUFFIX_RE = re.compile(r"#[0-9A-Za-z_-]{1,64}$")


def _claim_holder(assignee: str | None = None, session_id: str | None = None) -> str:
    """The identity a claim is recorded under.

    An explicit ``assignee`` always wins — callers that already have a better
    id (a worker name, a CI job) keep passing it.

    Otherwise the caller's identity is qualified with the agent session it came
    from, because the bare identity CANNOT TELL TWO OF ONE PERSON'S PARALLEL
    SESSIONS APART, and that is not a near-miss — it defeats the check
    entirely. With both sessions claiming as ``"Tobias Macey"``,
    ``task_claim``'s ``current_holder != holder`` test is False, so the
    contention branch never runs: the second session skips straight to the
    write, *renews the first session's lease*, and is told ``claimed: True``.
    Two agents then work the same task, each believing it holds an exclusive
    claim, and neither side ever sees a signal. Session-qualifying the holder
    turns that silent double-claim into an ordinary "held by someone else",
    which the elicit/force path already handles.

    ★ THE ID MUST COME FROM THE CALLER, NOT THIS PROCESS'S ENVIRONMENT. Reading
    ``$CLAUDE_SESSION_ID`` here works only under local stdio, where the server
    is a child of the agent and inherits it. A deployed pod has no such
    variable (its env is ``WITAN_*``/``KUBERNETES_*`` and nothing else), so
    every remote caller would fall back to the bare ``preferred_username`` and
    keep colliding — in the one topology where concurrent users are the whole
    point. ``witan_core.remote.proxy._map_args`` injects the client's id for
    tools declaring ``session_id``, the same way it already does for ``repo``
    and ``session_slug``; an agent calling a *deployed* witan directly (not
    through the CLI proxy) has to pass its own.

    The environment is still consulted as the local-stdio fallback. Truncated
    because the holder string is read by humans in refusal messages and task
    listings, and 8 hex chars is plenty to tell two concurrent sessions apart.

    With no session id from either source there is only one session to be, so
    the bare identity is both correct and byte-identical to what older stores
    already hold.
    """
    if assignee:
        return assignee
    identity = _current_author()
    session = session_id or os.environ.get("CLAUDE_SESSION_ID") or ""
    return f"{identity}#{session[:8]}" if session else identity


def _holder_identity(holder: str | None) -> str | None:
    """Strip a holder's ``#<session>`` qualifier, leaving the person."""
    return _SESSION_SUFFIX_RE.sub("", holder) if holder else holder


def _is_qualified(holder: str | None) -> bool:
    """Whether a holder string names a session as well as a person."""
    return bool(holder) and _SESSION_SUFFIX_RE.search(holder) is not None


def _holder_matches(recorded: str | None, wanted: str | None) -> bool:
    """Whether a row's holder satisfies an ``assignee`` *filter*.

    The filter's own precision decides the scope, which is the only reading
    that lets one parameter answer both questions people actually ask:

      ``"Tobias Macey"``           → every session of that person
      ``"Tobias Macey#5e313f6d"``  → that one session, and no other

    Stripping the qualifier off *both* sides — the obvious implementation, and
    the first one here — collapses the second case into the first: filtering
    for ``alice#aaaaaaaa`` would also return rows held by ``alice#bbbbbbbb``,
    so a qualified selector silently meant something wider than it says. Only
    an unqualified filter is widened to the person.
    """
    if wanted is None:
        return True
    if recorded == wanted:
        return True
    if _is_qualified(wanted):
        return False
    return _holder_identity(recorded) == wanted


def _same_person(a: str | None, b: str | None) -> bool:
    """Whether two holder strings name the same person, whatever the session.

    Deliberately *not* ``_holder_matches``: this asks about two holders rather
    than matching a row against a filter, and the answer must ignore the
    qualifier on both sides. ``task_release`` is the caller — releasing a claim
    that another of your own sessions took is a handover, not a steal, so it
    should not need ``force``, while another person's still should.
    """
    return _holder_identity(a) == _holder_identity(b)


def _lease_expiry(lease_started_at: str | None) -> str | None:
    """When an advisory lease lapses, ISO-8601, or ``None`` if unknowable."""
    if not lease_started_at:
        return None
    try:
        started = datetime.fromisoformat(lease_started_at)
    except (ValueError, TypeError):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (started + timedelta(seconds=readiness.CLAIM_LEASE_SECONDS)).isoformat()


def _claim_remedy(slug: str, held_by: str, lease_started_at: str | None) -> str:
    """What to actually *do* about a refused claim.

    A refusal that only names the holder is a dead end for the two cases that
    matter most: nobody is on record as the holder (the task was moved to
    ``in_progress`` by ``task_update``, so there is no person to go and ask),
    and the holder is a session of your own that has since gone away. Both are
    recoverable — ``force`` steals, ``task_release --force`` clears, and the
    lease lapses on its own — but none of that is discoverable from
    ``{"claimed": false, "reason": "held"}``, so callers conclude the task is
    simply stuck. Say the way out in the response, next to the refusal.
    """
    expiry = _lease_expiry(lease_started_at)
    when = f" Its lease lapses at {expiry}." if expiry else ""
    if held_by == _UNKNOWN_HOLDER:
        return (
            f"No holder is on record — {slug} was moved to in_progress without a "
            f"claim, so there is nobody to hand it over.{when} Take it with "
            f"`witan task claim {slug} --force`, or clear it with "
            f"`witan task release {slug} --force`."
        )
    return (
        f"{slug} is held by {held_by}.{when} Take it over with "
        f"`witan task claim {slug} --force` if that holder is gone."
    )


# Bounded re-tries for the best-effort CAS claim loop: on each surfaced
# optimistic-concurrency conflict we re-read and either bail (a rival won) or
# re-attempt the claim. Was 3 with no backoff between attempts — three
# back-to-back immediate re-attempts have no chance against the contention
# windows tk-the-write-gate-is-sized-against-a-3-45s-solo-wri-73fc2b measured
# (loaded writes taking 17-44s), so an unrelated conflict on a hot table (e.g.
# node:Task, written by every claim/update/close across every session) would
# exhaust this budget and escape as a raw OmnigraphConflict — see
# tk-task-claim-exhausts-its-3-attempt-no-backoff-cas-674414. Widened and
# paired with a short jittered backoff below; still deliberately short next to
# the 30s call deadline, since a real fix for multi-second contention is the
# write-gate/batching work, not a longer spin here.
_CLAIM_MAX_ATTEMPTS = 5
_CLAIM_BACKOFF_BASE = 0.25
_CLAIM_BACKOFF_CAP = 3.0


def _claim_backoff(attempt: int) -> float:
    """Jittered exponential backoff between CAS retry attempts.

    Jitter matters here specifically because the trigger case is a burst of
    concurrent claimers on the same task/table — lockstep exponential backoff
    would just have them collide again on the next attempt.
    """
    delay = _CLAIM_BACKOFF_BASE * (2 ** (attempt - 1))
    jitter = random.uniform(0, 0.1 * delay)
    return min(delay + jitter, _CLAIM_BACKOFF_CAP)


# Bounds for task_claim's post-write verification catch-up loop (see the
# comment at the call site) — sized above the largest staleness gap actually
# measured (~2s, tk-mutual-exclusion-violated-2-of-8-racers-both-got-52b3dd),
# so a genuinely-stale read path gets a real chance to catch up before the
# loop gives up and falls back to trusting whatever it last saw.
_VERIFY_CAUGHT_UP_MAX_ATTEMPTS = 6
_VERIFY_CAUGHT_UP_BACKOFF_SECONDS = 0.5


def _project_repos(row: dict) -> list[str]:
    """The repo set of a project/trace row (empty list when floating)."""
    return list(row.get("repos") or [])


def _merge_repos(*sources: list[str] | str | None) -> list[str]:
    """Union repo URIs from any mix of lists/scalars, dropping blanks/dupes,
    preserving first-seen order."""
    out: list[str] = []
    for src in sources:
        if not src:
            continue
        for repo in [src] if isinstance(src, str) else src:
            if repo and repo not in out:
                out.append(repo)
    return out


def _make_slug(kind: str, title: str) -> str:
    """Generate a stable, human-readable slug from kind and title."""
    prefix = _KIND_PREFIX.get(kind, "mem")
    sanitised = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48]
    short_id = uuid.uuid4().hex[:6]
    return f"{prefix}-{sanitised}-{short_id}"


_SLIM_KEYS = ("slug", "kind", "title", "tags")


def _slim_memory(m: dict) -> dict:
    """Return slug/kind/title/tags — enough to decide whether to fetch.

    Used when returning unscoped memories to an agent that hasn't opted into
    a full listing: the agent can scan slugs and call memory_get on the ones
    it actually needs rather than absorbing every word of every memory.
    """
    return {k: m[k] for k in _SLIM_KEYS if k in m}


def _topic_slug(kind: str, name: str) -> str:
    """Deterministic, idempotent slug for a Topic: ``tp-<kind>-<slug(name)>``.

    No random suffix — two stores of the same (kind, name) collide on the ``@key``
    and the second is a no-op, so a topic is created at most once. When the name
    has no ``[a-z0-9]`` characters to slugify (e.g. punctuation-only, or a
    non-Latin script like ``日本語``), fall back to a short content hash so
    distinct names don't all collapse to ``tp-<kind>-``.
    """
    sanitised = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:64]
    if not sanitised:
        sanitised = hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]
    return f"tp-{kind}-{sanitised}"


def _upsert_topic_steps(
    name: str, kind: str
) -> tuple[str, list[tuple[str, str, dict]]]:
    """Return the Topic slug for (name, kind) and the steps that create it.

    The steps are empty when the Topic already exists, which is also how callers
    (e.g. ``migrate_topics``) count creations without a second ``get_topic``
    read. The existence read happens here; only the write is deferred, so the
    caller can commit it alongside whatever else it is writing.
    """
    slug = _topic_slug(kind, name)
    if client.read("read.gq", "get_topic", {"slug": slug}):
        return slug, []
    return slug, [
        (
            "mutations.gq",
            "insert_topic",
            {"slug": slug, "name": name, "kind": kind, "created_at": now_iso()},
        )
    ]


def _resolve_topic_steps(ref: str) -> tuple[str | None, list[tuple[str, str, dict]]]:
    """Resolve a topic reference to a slug, with the steps to create it if needed.

    ``ref`` is either an existing Topic slug (``tp-...``) or a ``name:kind`` spec
    (e.g. ``cryptography:topic`` or ``GET /api/v1/courses/:contract``). The slug is
    ``None`` when ``ref`` is a slug that does not resolve to a Topic.
    """
    # Only a tp- slug can resolve directly; skip the read for a name:kind spec.
    if ref.startswith("tp-") and client.read("read.gq", "get_topic", {"slug": ref}):
        return ref, []
    name, sep, kind = ref.rpartition(":")
    if sep and kind in ("topic", "contract", "symbol", "entity") and name:
        return _upsert_topic_steps(name, kind)
    return None, []


def _lookup_topic_slug(topic: str) -> str | None:
    """Resolve a topic ref (slug or ``name:kind``) to a slug WITHOUT creating it."""
    if client.read("read.gq", "get_topic", {"slug": topic}):
        return topic
    name, sep, kind = topic.rpartition(":")
    if sep and kind in ("topic", "contract", "symbol", "entity") and name:
        rows = client.read(
            "read.gq", "topic_by_name_kind", {"name": name, "kind": kind}
        )
        return rows[0]["slug"] if rows else None
    return None


def _tag_memory_steps(
    memory_slug: str, name: str, kind: str
) -> list[tuple[str, str, dict]]:
    """Upsert a Topic and link ``memory_slug`` to it, as batchable steps."""
    slug, steps = _upsert_topic_steps(name, kind)
    return [*steps, ("mutations.gq", "link_tagged", {"from": memory_slug, "to": slug})]


# Statements per commit in the topic backfill. Bounds the inline GQ body — the
# composed query is passed as an argv element, so this trades a handful of extra
# commits for staying well clear of ARG_MAX and any server payload cap.
#
# A SOFT threshold, checked between memories rather than between statements: a
# chunk may overshoot by up to one memory's worth of steps. Keeping each
# memory's steps together is worth more than an exact cap, and the overshoot is
# bounded by how many tags one memory can carry.
_MIGRATE_BATCH_SIZE = 100


def migrate_topics() -> dict:
    """One-shot, idempotent backfill: promote every memory ``tag`` to a
    ``Topic{kind:"topic"}`` + ``Tagged`` edge.

    Safe to re-run — topic upsert is keyed on slug and the existing-edge check
    skips already-linked (memory, topic) pairs. Returns counts for reporting.
    """
    # Slim read: only slug + tags, not full memory content.
    rows = client.read("read.gq", "list_memory_tags", {})
    topics_created = 0
    edges_created = 0
    seen_topics: set[str] = set()
    # This sweeps the WHOLE store, so writing per row is how a backfill leaves
    # behind the fragmented store the batching exists to avoid — a few hundred
    # memories is a few hundred Lance versions. Steps are flushed in bounded
    # chunks rather than one batch: the composed query is inline argv, so an
    # unbounded body would eventually exceed the command-line/payload limit.
    # Chunks commit in order and a Topic insert is always appended before the
    # edges naming it, so a topic never lands in a later chunk than its edge.
    steps: list[tuple[str, str, dict]] = []

    def flush() -> None:
        if steps:
            client.change_many(list(steps))
            steps.clear()

    for row in rows:
        slug = row["slug"]
        existing = {
            t["slug"]
            for t in client.read("read.gq", "topics_for_memory", {"slug": slug})
        }
        for tag in dict.fromkeys(t for t in (row.get("tags") or []) if t.strip()):
            topic_slug = _topic_slug("topic", tag)
            if topic_slug not in seen_topics:
                _, create_steps = _upsert_topic_steps(tag, "topic")
                if create_steps:
                    topics_created += 1
                    steps += create_steps
                seen_topics.add(topic_slug)
            if topic_slug not in existing:
                steps.append(
                    ("mutations.gq", "link_tagged", {"from": slug, "to": topic_slug})
                )
                edges_created += 1
        if len(steps) >= _MIGRATE_BATCH_SIZE:
            flush()
    flush()
    return {
        "memories_scanned": len(rows),
        "topics_created": topics_created,
        "edges_created": edges_created,
    }


_AUTOCLOSE_PREFIX = "Session ended (auto-closed by Stop hook"


def _has_own_summary(session: dict) -> bool:
    """True when a session carries a summary someone actually wrote.

    The Stop hook's auto-close placeholder is treated as no summary: it says
    only that the session ended, so a session carrying it holds nothing the
    corpus would lose.
    """
    summary = (session.get("summary") or "").strip()
    return bool(summary) and not summary.startswith(_AUTOCLOSE_PREFIX)


def _overlap_runs(sessions: list[dict]) -> list[list[dict]]:
    """Split one (project, session_id) group into runs of overlapping sessions.

    A run is a session plus every later one that started while **any** member of
    the run was still open — exactly the condition ``workflow_session_start``
    now refuses to create. A session starting after every member has ended opens
    a new run instead: it's the next working stint of the same
    ``$CLAUDE_SESSION_ID``, not a duplicate. Single-element runs are dropped;
    they have nothing to reconcile.

    Overlap is transitive, hence tracking the run's furthest end rather than
    comparing against its first session: given s1 [10:00-10:10] and a retry
    s2 [10:05-10:20], a third session starting 10:12 is still a duplicate — s1
    had ended, but s2 was open, so the fixed ``workflow_session_start`` would
    have handed back s2's handle. A member that never ended (``has_open``)
    leaves the run open indefinitely.
    """
    runs: list[list[dict]] = []
    current: list[dict] = []
    has_open = False
    max_end = ""

    for s in sorted(sessions, key=lambda r: r.get("started_at") or ""):
        overlaps = has_open or (s.get("started_at") or "") < max_end
        if current and not overlaps:
            if len(current) > 1:
                runs.append(current)
            current, has_open, max_end = [], False, ""
        current.append(s)
        end = s.get("ended_at")
        if end:
            max_end = max(max_end, end)
        else:
            has_open = True
    if len(current) > 1:
        runs.append(current)
    return runs


def migrate_dedupe_sessions(
    apply: bool = False, extra_marks: dict[str, str] | None = None
) -> dict:
    """Reconcile WorkflowSessions a pre-upsert ``workflow_session_start`` minted.

    Before that fix, every call minted a node, so a hook retry, a transport
    reconnect, or a deliberate re-call (the only way there was to widen a
    project's repo set) left extra sessions sharing one ``session_id``.
    ``workflow_project_complete`` counts every linked session into its trace,
    so those extras inflate the corpus.

    Sharing a ``session_id`` is *not* on its own evidence of duplication: one
    ``$CLAUDE_SESSION_ID`` routinely spans several working stints, each closed
    with its own summary. So this only considers sessions that **overlap in
    time** — the retry signature — and within an overlapping run only flags
    members with no summary of their own, keeping whichever member has the
    fullest summary as the survivor. A run whose members all wrote real
    summaries is left completely alone and reported for a human to judge;
    ``extra_marks`` is how that judgment gets applied.

    Nothing is deleted: a flagged session keeps its row and its edges and only
    gains ``superseded_by``, which every aggregate read then skips.

    Dry by default — pass ``apply=True`` to write. Idempotent: an
    already-flagged session is skipped, and re-running finds nothing new.

    Parameters
    ----------
    apply:
        Write the ``superseded_by`` marks. Without it, only report.
    extra_marks:
        ``{duplicate_slug: survivor_slug}`` to mark regardless of the automatic
        rule — for the ambiguous runs reported as needing review.
    """
    try:
        rows = client.read("read.gq", "list_all_sessions_key", {})
    except RuntimeError as exc:
        # A store that hasn't applied the schema since `superseded_by` was added
        # raises an opaque engine type error. Say what to run instead.
        if "superseded_by" in str(exc):
            raise RuntimeError(
                "This store predates the WorkflowSession.superseded_by field. "
                "Run `witan migrate schema` first."
            ) from None
        raise

    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        if row.get("superseded_by"):
            continue
        key = (row.get("project_slug") or "", row.get("session_id") or "")
        groups.setdefault(key, []).append(row)

    marks: dict[str, str] = {}
    needs_review: list[dict] = []
    for (project_slug, session_id), group in groups.items():
        if len(group) < 2:
            continue
        for run in _overlap_runs(group):
            with_summary = [s for s in run if _has_own_summary(s)]
            # Survivor: the fullest account of the work, else the earliest.
            survivor = (
                max(with_summary, key=lambda s: len(s.get("summary") or ""))
                if with_summary
                else run[0]
            )
            for s in run:
                if s["slug"] != survivor["slug"] and not _has_own_summary(s):
                    marks[s["slug"]] = survivor["slug"]
            if len(with_summary) > 1:
                needs_review.append(
                    {
                        "project_slug": project_slug,
                        "session_id": session_id,
                        "sessions": [
                            {
                                "slug": s["slug"],
                                "started_at": s.get("started_at"),
                                "ended_at": s.get("ended_at"),
                                "summary": (s.get("summary") or "")[:160],
                            }
                            for s in run
                            if _has_own_summary(s)
                        ],
                    }
                )

    by_slug = {r["slug"]: r for r in rows}
    for dup_slug, survivor_slug in (extra_marks or {}).items():
        if dup_slug not in by_slug:
            raise RuntimeError(f"no such session: {dup_slug}")
        if survivor_slug not in by_slug:
            raise RuntimeError(f"no such session: {survivor_slug}")
        if dup_slug == survivor_slug:
            raise RuntimeError(f"a session cannot supersede itself: {dup_slug}")
        marks[dup_slug] = survivor_slug

    # A project whose trace is already sealed can't be recounted — the trace is
    # immutable by design. Report it so the skew is at least known.
    sealed: list[str] = []
    if marks:
        affected = {by_slug[dup]["project_slug"] for dup in marks if dup in by_slug}
        for project_slug in sorted(p for p in affected if p):
            if client.read("read.gq", "get_trace", {"slug": f"wt-{project_slug}"}):
                sealed.append(project_slug)

    if apply:
        for dup_slug, survivor_slug in marks.items():
            client.change(
                "mutations.gq",
                "update_workflow_session_superseded",
                {"slug": dup_slug, "superseded_by": survivor_slug},
            )

    return {
        "sessions_scanned": len(rows),
        "marked": marks,
        "needs_review": needs_review,
        "sealed_traces": sealed,
        "applied": apply,
    }


def _fold_symbol_ref(ref: str) -> str:
    """Case-fold the repo prefix of a soft symbol ref (``repo#path::Name``)."""
    repo_part, sep, rest = ref.partition("#")
    return f"{normalise(repo_part)}{sep}{rest}" if sep else ref


def migrate_repo_keys() -> dict:
    """One-shot, idempotent repo-key case-fold migration (issue #142).

    ``normalise`` (``witan_core.repo_key``) now case-folds GitHub/GitLab
    ``repo`` keys — host always, org/repo path for those two hosts — so the
    same remote canonicalizes identically regardless of how it's spelled;
    ``repo.detect`` now routes every resolution path (override, ``WITAN_REPO``,
    git remote) through it. Rows written before that fix may still carry a
    stale, differently-cased ``repo``/``repos``/symbol-ref value that no
    longer matches what ``repo.detect`` returns for the same remote — silently
    dropping them out of every repo-scoped read (``task_ready``,
    ``memory_list``, ...). This rewrites every repo-keyed field in place,
    using ``normalise`` as the source of truth for "canonical".

    ``CodeBranch`` is the one exception: its slug embeds ``repo`` (``@key``),
    so it can't be updated in place. A canonical replacement is inserted if
    one doesn't already exist (e.g. a session may have created it via
    ``task_claim`` after the case-fold fix shipped but before this migration
    ran), its ``WorksOn``/``ForProject`` edges are merged onto that slug —
    existing-edge checks keep this dedup-safe whether the canonical branch is
    freshly inserted or pre-existing — else the "In-Flight Branch" context and
    ``task_code_branches`` silently lose the association once reads move to
    the canonical slug, and the stale row is marked ``abandoned`` rather than
    deleted (no delete mutation exists for it). Idempotency for this section
    is keyed on the stale row's own status (skip once it's ``abandoned``), not
    on whether the canonical branch exists — the latter alone would skip the
    edge-merge step on a second run against the scenario above.

    Does NOT touch the code graph (witan-code): its per-repo stores and symbol
    ids are documented as re-derivable caches, so the fix there is
    ``witan-code reindex``, not a migration — the returned ``repos_changed``
    map is which repos need it.

    Safe to re-run — every row is skipped once its key is already canonical.
    """
    now = now_iso()
    counts = {
        "tasks_updated": 0,
        "memories_updated": 0,
        "sessions_updated": 0,
        "projects_updated": 0,
        "traces_updated": 0,
        "code_branches_migrated": 0,
    }
    repos_changed: dict[str, str] = {}

    def _note(old: str | None, new: str) -> None:
        if old and old != new:
            repos_changed[old] = new

    for row in client.read("read.gq", "list_all_tasks_full", {}):
        repo = row.get("repo")
        refs = row.get("symbol_refs")
        new_repo = normalise(repo) if repo else repo
        new_refs = [_fold_symbol_ref(r) for r in refs] if refs else refs
        if new_repo == repo and new_refs == refs:
            continue
        _note(repo, new_repo)
        client.change(
            "mutations.gq",
            "update_task",
            {**row, "repo": new_repo, "symbol_refs": new_refs, "updated_at": now},
        )
        counts["tasks_updated"] += 1

    for row in client.read("read.gq", "list_all_memories_full", {}):
        repo = row.get("repo")
        refs = row.get("symbol_refs")
        new_repo = normalise(repo) if repo else repo
        new_refs = [_fold_symbol_ref(r) for r in refs] if refs else refs
        if new_repo == repo and new_refs == refs:
            continue
        _note(repo, new_repo)
        client.change(
            "mutations.gq",
            "update_memory",
            {**row, "repo": new_repo, "symbol_refs": new_refs, "updated_at": now},
        )
        counts["memories_updated"] += 1

    for row in client.read("read.gq", "list_all_sessions_repo", {}):
        repo = row.get("repo")
        if not repo:
            continue
        new_repo = normalise(repo)
        if new_repo == repo:
            continue
        _note(repo, new_repo)
        client.change(
            "mutations.gq",
            "update_workflow_session_repo",
            {"slug": row["slug"], "repo": new_repo},
        )
        counts["sessions_updated"] += 1

    for row in client.read("read.gq", "list_all_projects", {}):
        repos = row.get("repos") or []
        pairs = [(r, normalise(r)) for r in repos]
        folded = _merge_repos([new for _, new in pairs])
        if folded == repos:
            continue
        for old, new in pairs:
            _note(old, new)
        client.change(
            "mutations.gq",
            "update_workflow_project_repos",
            {"slug": row["slug"], "repos": folded, "updated_at": now},
        )
        counts["projects_updated"] += 1

    for row in client.read("read.gq", "list_all_traces", {}):
        repos = row.get("repos") or []
        pairs = [(r, normalise(r)) for r in repos]
        folded = _merge_repos([new for _, new in pairs])
        if folded == repos:
            continue
        for old, new in pairs:
            _note(old, new)
        client.change(
            "mutations.gq",
            "update_workflow_trace_repos",
            {"slug": row["slug"], "repos": folded},
        )
        counts["traces_updated"] += 1

    for row in client.read("read.gq", "list_all_code_branches", {}):
        repo = row["repo"]
        canonical = normalise(repo)
        # Idempotency is keyed on the STALE row's own status, not on whether
        # the canonical branch already exists — a session can legitimately
        # create the canonical branch (e.g. via task_claim) after the
        # case-fold fix shipped but before this migration runs, and that
        # branch still needs the stale row's edges merged onto it below.
        if canonical == repo or row["status"] == "abandoned":
            continue
        new_slug = _code_branch_slug(canonical, row["branch"])
        _note(repo, canonical)
        if not client.read("read.gq", "get_code_branch", {"slug": new_slug}):
            client.change(
                "mutations.gq",
                "insert_code_branch",
                {
                    "slug": new_slug,
                    "repo": canonical,
                    "branch": row["branch"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "updated_at": now,
                },
            )
        for task in client.read(
            "read.gq", "code_branch_tasks", {"branch_slug": row["slug"]}
        ):
            if not client.read(
                "read.gq",
                "code_branch_works_on_edge",
                {"branch_slug": new_slug, "task_slug": task["slug"]},
            ):
                client.change(
                    "mutations.gq",
                    "link_works_on",
                    {"from": new_slug, "to": task["slug"]},
                )
        for project in client.read(
            "read.gq", "code_branch_projects", {"branch_slug": row["slug"]}
        ):
            if not client.read(
                "read.gq",
                "code_branch_for_project_edge",
                {"branch_slug": new_slug, "project_slug": project["slug"]},
            ):
                client.change(
                    "mutations.gq",
                    "link_for_project",
                    {"from": new_slug, "to": project["slug"]},
                )
        client.change(
            "mutations.gq",
            "touch_code_branch",
            {"slug": row["slug"], "status": "abandoned", "updated_at": now},
        )
        counts["code_branches_migrated"] += 1

    return {**counts, "repos_changed": repos_changed}


# ── Storage-format migration ────────────────────────────────────

# _is_storage_version_mismatch is imported from .graph — the same detector
# OmnigraphClient uses to turn a raw Rust panic into a friendly, actionable
# error for every read/change/apply_schema call (not just this module).


def _snapshot(binary: str, store: str) -> tuple[bool, str]:
    """Run ``<binary> snapshot --store <store>``; returns (ok, output).

    On failure, stdout and stderr are both included — the mismatch text isn't
    guaranteed to land on stderr alone.
    """
    result = subprocess.run(
        [binary, "snapshot", "--store", store], capture_output=True, text=True
    )
    if result.returncode == 0:
        return True, result.stdout
    return False, "\n".join(s for s in (result.stdout, result.stderr) if s)


def _pre_upgrade_binary_candidates(current_binary: str) -> list[str]:
    """Binaries that might still read a store the current one refuses.

    A LIST, TRIED IN ORDER, not a single answer — because no single candidate
    is knowably the right one:

    * The installer's set-aside copies (``omnigraph-<version>``) are the best
      guesses, newest first, but only for stores in that install's lineage. A
      machine can hold stores at several formats at once: witan-code keeps one
      ``<slug>.omni`` per repository, migrated only when someone next opens
      that repo, so an untouched repo can be two releases behind.
    * A PATH hit may be an unrelated Homebrew or system install of unknown
      vintage. It cannot be preferred — but neither can it be skipped, since
      ``OmnigraphClient._find_binary`` resolves PATH *before* ``~/.local/bin``,
      so a Homebrew binary can legitimately be the current one while a stale
      set-aside copy sits unused. Returning that copy alone would abort the
      migration without ever looking at PATH.

    Ordering is preference, not correctness: the caller proves a candidate by
    opening the store with it (:func:`migrate_storage_format`), so a wrong
    guess costs one failed ``snapshot`` and moves on.

    Whatever the current binary resolves to is excluded — it is by definition
    the one that cannot read this store.
    """
    current_real = Path(current_binary).resolve()
    candidates: list[str] = []
    seen: set[Path] = {current_real}

    def offer(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved in seen:
            return
        seen.add(resolved)
        candidates.append(str(path))

    for preserved in omnigraph_install.preserved_binaries():
        offer(preserved)
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry) / "omnigraph"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            offer(candidate)
    return candidates


def _run_omnigraph(cmd: list[str], *, label: str, stdout=None) -> str:
    """Run an omnigraph subcommand with no retry policy. ``migrate_storage_format`` only.

    When ``stdout`` is an open file, output streams straight there (so a
    large ``export`` never sits fully buffered in memory) and ``""`` is
    returned; otherwise stdout is captured and returned as a string.

    **Bypassing ``OmnigraphClient`` here is deliberate, and it is the only place
    that is true.** The storage-format rebuild runs the *old* omnigraph binary
    against the store — a client is bound to the one binary it resolved at
    construction, so it cannot express "export this with the binary that last
    wrote it". The steps that use the new binary target a local scratch store
    that does not exist yet, which no client addresses either.

    Skipping the retry policy costs nothing here because every store involved is
    local: ``migrate_storage_format`` refuses an http(s)/s3 store outright, and
    for a local store the policy classifies a connect failure as FATAL rather
    than waiting — there is no server to come back. Every *other* call site,
    where a store may be remote and a restart is routine, goes through the
    client (see :func:`_store_client`).
    """
    result = subprocess.run(
        cmd,
        stdout=stdout if stdout is not None else subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"omnigraph {label} failed:\n{(result.stderr or '').strip()}"
        )
    return result.stdout or ""


def _acquire_store_lock(store: str):
    """Hold the same ``<store>.lock`` writers use (``OmnigraphClient``) for the
    full rebuild+swap, so a concurrent witan write can't race the migration.

    Goes through ``witan_core``'s re-entrant helper rather than its own
    ``flock``: a second ``open()`` of one lock file blocks even the thread
    already holding it, so a plain nested acquisition self-deadlocks. Nothing
    inside the rebuild takes it today (it drives the CLI directly, see
    ``_run_omnigraph``), but sharing the primitive is what keeps that true if
    something ever does.
    """
    return acquire_store_flock(store)


def migrate_storage_format(old_binary: str | None = None) -> dict:
    """Rebuild the configured local store when the current omnigraph binary
    refuses to open it because it was written by an older, incompatible
    on-disk format (a strict single-version storage bump, e.g. 0.7 → 0.8).

    Replays the rebuild omnigraph's upgrade docs prescribe: ``schema show``
    + ``export`` with the *old* binary, ``init`` + ``load --mode overwrite``
    with the *new* one, into a scratch store. Verifies the rebuilt store
    opens, then swaps it in and renames the original aside as
    ``<store>.pre-migrate`` rather than deleting it. Node/edge rows, vectors,
    and blobs survive; commit history and branches do not.

    Returns ``{"migrated": False, "reason": ...}`` when the current binary
    already opens the store fine (nothing to do). Raises ``RuntimeError`` for
    a genuinely broken store, a missing/incapable old binary, or any failed
    step of the rebuild.
    """
    store = client.graph_uri
    if store.startswith(("http://", "https://", "s3://")):
        raise RuntimeError(
            f"{store} is a remote store — rebuild it by hand with the old and "
            "new omnigraph binaries per the upgrade docs "
            "(docs/user/operations/upgrade.md); this command only handles "
            "local on-disk stores."
        )

    new_binary = client._binary
    ok, out = _snapshot(new_binary, store)
    if ok:
        return {
            "migrated": False,
            "reason": "already readable by the current omnigraph binary",
        }
    if not _is_storage_version_mismatch(out):
        raise RuntimeError(
            f"omnigraph snapshot failed for an unrelated reason:\n{out.strip()}"
        )

    # An explicit --old-binary is taken at its word and not probed against the
    # alternatives: the caller is asserting which release wrote this store, and
    # silently falling through to a different one would hide their mistake.
    candidates = (
        [old_binary] if old_binary else _pre_upgrade_binary_candidates(new_binary)
    )
    old, old_out = None, ""
    for candidate in candidates:
        # PROVE each candidate by opening the store with it. Which binary can
        # read a given store is not decidable from names or versions — a
        # machine holds many stores at different formats (witan-code keeps one
        # per repo), so the only reliable test is the one the engine performs.
        ok, out = _snapshot(candidate, store)
        if ok:
            old = candidate
            break
        old_out = out

    if old is None:
        if not candidates:
            raise RuntimeError(
                "The store was written by an older, incompatible omnigraph "
                "on-disk format, and no other binary was found to export it — "
                "neither a set-aside `omnigraph-<version>` beside "
                f"{omnigraph_install.default_install_path()} nor another "
                "`omnigraph` on PATH. This happens when the store predates the "
                "upgrade that started preserving the outgoing binary. Download "
                "the release that last wrote this store from "
                "https://github.com/ModernRelay/omnigraph/releases and pass "
                "its path."
            )
        tried = ", ".join(candidates)
        raise RuntimeError(
            f"No available omnigraph binary can read this store. Tried: "
            f"{tried}. The last attempt reported:\n{old_out.strip()}\n\n"
            "If the store predates every binary still on this machine, "
            "download the release that last wrote it from "
            "https://github.com/ModernRelay/omnigraph/releases and pass its "
            "path."
        )

    store_path = Path(store)
    lock_fh = _acquire_store_lock(store)
    try:
        # Scratch dir shares the store's filesystem (not /tmp, which is often a
        # size-limited tmpfs): keeps a large export off a RAM disk and makes the
        # final swap below a same-device rename instead of a copy.
        with tempfile.TemporaryDirectory(
            dir=store_path.parent, prefix=".witan-migrate-"
        ) as tmp:
            tmp_path = Path(tmp)
            schema_file = tmp_path / "schema.pg"
            data_file = tmp_path / "graph.jsonl"
            rebuilt = tmp_path / "rebuilt.omni"

            schema_file.write_text(
                _run_omnigraph(
                    [old, "schema", "show", "--store", store],
                    label="schema show (old binary)",
                )
            )
            with open(data_file, "w", encoding="utf-8") as f:
                _run_omnigraph(
                    [old, "export", "--store", store],
                    label="export (old binary)",
                    stdout=f,
                )
            _run_omnigraph(
                [new_binary, "init", "--schema", str(schema_file), str(rebuilt)],
                label="init (new binary)",
            )
            _run_omnigraph(
                [
                    new_binary,
                    "load",
                    "--store",
                    str(rebuilt),
                    "--data",
                    str(data_file),
                    "--mode",
                    "overwrite",
                ],
                label="load (new binary)",
            )

            verify_ok, verify_out = _snapshot(new_binary, str(rebuilt))
            if not verify_ok:
                raise RuntimeError(
                    f"Rebuilt store failed verification:\n{verify_out.strip()}"
                )

            backup_path = store_path.with_name(store_path.name + ".pre-migrate")
            if backup_path.exists():
                raise RuntimeError(
                    f"Backup path {backup_path} already exists from a previous "
                    "migration attempt; remove or rename it before retrying."
                )
            store_path.rename(backup_path)
            try:
                shutil.move(str(rebuilt), str(store_path))
            except OSError:
                # Restore the original so the store path isn't left empty.
                backup_path.rename(store_path)
                raise
    finally:
        release_store_flock(store, lock_fh)

    return {
        "migrated": True,
        "store": str(store_path),
        "backup": str(backup_path),
        "old_binary": old,
        "new_binary": new_binary,
        "verify": verify_out.strip(),
    }


# ── Cross-store merge (docs/migration-runbook.md) ──────────────────

# `omnigraph load --mode merge` overwrites unconditionally on a slug `@key`
# collision (last-loaded-file wins) — verified empirically, see the migration
# runbook. It has no notion of "newer" data. The fields below are, per node
# type, the ones that actually change on every write — reconciliation prefers
# them, falling back down the tuple when a row predates that field or the
# type has no `updated_at` at all (WorkflowSession/WorkflowTrace/Topic).
_RECONCILE_TS_FIELDS: dict[str, tuple[str, ...]] = {
    "Memory": ("updated_at", "created_at"),
    "WorkflowProject": ("updated_at", "created_at"),
    "Task": ("updated_at", "created_at"),
    "CodeBranch": ("updated_at", "created_at"),
    "WorkflowSession": ("ended_at", "started_at"),
    "WorkflowTrace": ("created_at",),
    "Topic": ("created_at",),
}


def _reconcile_timestamp(row_type: str, data: dict) -> str | int | float | None:
    """The best available comparison timestamp for a row, or ``None`` if it has
    none of its type's candidate fields set (an in-progress WorkflowSession
    with no ``ended_at`` falls back to ``started_at``, not to nothing).

    Returned as exported, not parsed — see :func:`_parse_ts` for the two
    representations that can come back, and note that this value is also
    echoed verbatim into the merge report's ``source_ts``/``target_ts``."""
    for field in _RECONCILE_TS_FIELDS.get(row_type, ("updated_at", "created_at")):
        value = data.get(field)
        if value:
            return value
    return None


#: omnigraph >= 0.9 exports a ``DateTime`` as integer milliseconds since the
#: Unix epoch, UTC. NOT microseconds — ``commit list --json`` uses microseconds
#: for its own ``created_at`` (see ``witan_code.graph.branch_last_write``, which
#: divides by 1_000_000), and the two surfaces genuinely disagree. Getting this
#: scale wrong does not raise; it silently dates every row to January 1970 and
#: quietly inverts merge decisions. Measured on 0.9.0:
#: ``"2026-01-01T00:00:00Z"`` exports as ``1767225600000``.
_EXPORT_TS_PER_SECOND = 1_000


def _parse_ts(value: str | int | float | None) -> datetime | None:
    """Parse an exported timestamp for comparison, or ``None`` if absent/unusable.

    TWO REPRESENTATIONS, because a merge routinely spans omnigraph versions —
    that is the whole point of the command. A store exported by 0.8.x yields
    naive ISO-8601 strings; 0.9.0 onward yields integer epoch milliseconds
    (``_EXPORT_TS_PER_SECOND``). ``witan migrate merge`` accepts a ``.jsonl``
    export taken on another machine, so a 0.8.x export can arrive long after
    every live store has moved to 0.9.x, and both forms have to keep working
    for as long as anyone holds an old export file.

    Everything normalizes to naive UTC so any two values are comparable —
    ``datetime`` raises on comparing an aware value against a naive one. The
    string form needs this because two stores' text isn't guaranteed to share a
    format (``Z`` vs. ``+00:00``, or genuinely different offsets), and
    comparing raw strings sorts wrong across those: ``"...T23:30:00-05:00"``,
    later in UTC, sorts *before* ``"...T00:00:00Z"`` the next calendar day.

    An unusable value degrades to ``None`` (treated as "no usable timestamp")
    rather than raising, since a malformed value shouldn't crash a merge — it
    just can't win a comparison. ``bool`` is excluded deliberately: it is an
    ``int`` subclass, and ``True`` would otherwise read as 1ms past the epoch.
    """
    if not value:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(
                value / _EXPORT_TS_PER_SECOND, timezone.utc
            ).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _classify_rows(
    rows: Iterable[dict], source: str
) -> tuple[dict[tuple[str, str], dict], list[dict]]:
    """Split ``omnigraph export`` records into reconcilable nodes and pass-through rows.

    An export holds two record shapes, and they do not share a discriminator:
    a node is ``{"type": <Node>, "data": {…}}`` while an edge is
    ``{"edge": <Edge>, "from": …, "to": …, "data": {…}}`` — an edge carries no
    ``"type"`` at all. Only nodes are reconciled, keyed ``(type, slug)``;
    everything else passes through the merge exactly as exported, because
    reconciliation needs a slug to match on and an edge has none. A ``type``
    row without a slug lands in the pass-through list for the same reason.

    This is the single classifier for both merge transports — ``_parse_export``
    (a file, in-process) and ``store_merge`` (a batch, over the wire). They had
    diverged, and the divergence was the bug: the file path treated a missing
    ``"type"`` as corruption and raised on the first edge, so a merge sourced
    from or targeting any graph with edges failed outright. ``source`` names
    what is being classified so the surviving error still points at a file.
    """
    nodes: dict[tuple[str, str], dict] = {}
    passthrough: list[dict] = []
    for row in rows:
        # A JSONL line is only *conventionally* an object: `[]`, `null`, `3`
        # and `"…"` all parse fine and would reach `.get` as an AttributeError,
        # which is the raw fault this boundary exists to convert into a
        # sentence. Checked here rather than in `_parse_export` so the wire
        # path gets it too — the MCP schema says `list[dict]`, but that is the
        # deployment's guarantee, not this function's.
        if not isinstance(row, dict):
            raise RuntimeError(f"{source}: export row is not a JSON object: {row!r}")
        row_type = row.get("type")
        if not row_type:
            if not row.get("edge"):
                raise RuntimeError(
                    f"{source}: export row is neither a node (no 'type') nor an "
                    f"edge (no 'edge'): {row!r}"
                )
            passthrough.append(row)
            continue
        slug = (row.get("data") or {}).get("slug")
        if slug:
            nodes[(row_type, slug)] = row
        else:
            passthrough.append(row)
    return nodes, passthrough


def _parse_export(path: Path) -> tuple[dict[tuple[str, str], dict], list[dict]]:
    """Read an ``omnigraph export`` JSONL file into ``_classify_rows``'s split.

    An export is an external boundary (another process's output, not
    necessarily produced by this run), so a malformed line raises a clear
    ``RuntimeError`` naming the offending line rather than a raw
    ``JSONDecodeError``/``KeyError`` mid-reconciliation."""

    def _lines() -> Iterator[dict]:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"{path}: corrupted export line, not valid JSON: {line!r}"
                    ) from exc

    return _classify_rows(_lines(), str(path))


def _reconcile_nodes(
    source_nodes: dict[tuple[str, str], dict],
    target_nodes: dict[tuple[str, str], dict],
) -> tuple[list[dict], list[dict]]:
    """Newest-record-wins reconciliation: ``(decisions, rows to write)``.

    The single implementation of the merge rule, shared by the in-process path
    (``merge_store``, which exports both stores itself) and the MCP-tier path
    (``store_merge``, where the source rows arrive over the wire and the target
    is the deployed graph). They must not drift: a row's fate should not depend
    on which transport carried it.
    """
    decisions: list[dict] = []
    winners: list[dict] = []
    for (row_type, slug), row in source_nodes.items():
        existing = target_nodes.get((row_type, slug))
        if existing is None:
            decisions.append({"type": row_type, "slug": slug, "decision": "added"})
            winners.append(row)
            continue
        src_ts = _reconcile_timestamp(row_type, row["data"])
        dst_ts = _reconcile_timestamp(row_type, existing["data"])
        src_dt = _parse_ts(src_ts)
        dst_dt = _parse_ts(dst_ts)
        won = src_dt is not None and (dst_dt is None or src_dt > dst_dt)
        decisions.append(
            {
                "type": row_type,
                "slug": slug,
                "decision": "updated" if won else "kept-target",
                "source_ts": src_ts,
                "target_ts": dst_ts,
            }
        )
        if won:
            winners.append(row)
    return decisions, winners


def _decision_counts(decisions: list[dict]) -> dict:
    return {
        "added": sum(1 for d in decisions if d["decision"] == "added"),
        "updated": sum(1 for d in decisions if d["decision"] == "updated"),
        "kept_target": sum(1 for d in decisions if d["decision"] == "kept-target"),
    }


def _store_client(uri: str) -> OmnigraphClient:
    """An ``OmnigraphClient`` addressing ``uri`` — a store that need not be the
    configured one.

    A merge touches two stores, either of which may be a *remote*
    omnigraph-server, and a remote call has to run under the shared
    retry/classification policy: that Deployment is ``replicas=1`` +
    ``strategy=Recreate``, so every restart is a hard endpoint gap, and a bare
    ``subprocess.run`` turns a routine one into a dead merge with a raw connect
    refusal. Addressing each side through a client is what keeps this path
    inside the policy rather than beside it.

    The configured store's own token is reused when ``uri`` names it (the
    in-cluster maintenance case, where ``WITAN_MEMORY_TOKEN`` is the only
    credential in the pod); any *other* remote store falls back to the ambient
    ``OMNIGRAPH_BEARER_TOKEN``, the omnigraph CLI's documented spelling — which
    is what passing ``token=None`` leaves in place (see ``store_subprocess_env``).

    "Names it" is decided on the *resolved* address, not on the raw string. A
    deployment sets ``WITAN_MEMORY_URI`` to a bare server URL plus a separate
    ``WITAN_MEMORY_GRAPH``, while the runbook spells a deployed graph
    ``http://host:8080/graphs/<id>`` — two spellings of one graph, and a raw
    string compare would drop the only credential the pod has for it.

    The configured graph id fills in a bare server URL only when that URL *is*
    the configured server; another server's URI must carry its own, or the
    merge would silently address a graph nobody named. A URI that ends up with
    no graph id at all is a caller error, so it surfaces as a ``RuntimeError``
    (what every caller of this module, the CLI included, already handles)
    rather than the raw ``ValueError`` from the parser.
    """
    fallback = (
        client.graph_id
        if client.is_remote and uri.rstrip("/") == client.server_url
        else None
    )
    try:
        args = store_cli_args(uri, fallback)
    except ValueError as exc:
        raise RuntimeError(f"{uri}: {exc}") from exc
    configured = store_cli_args(client.graph_uri, client.graph_id)
    token = client.token if args == configured else None
    return OmnigraphClient(
        uri, cfg.queries_dir, token, guard=_write_guard, graph_id=fallback
    )


def _is_export_file(source: str) -> bool:
    """Whether ``source`` names an ``omnigraph export`` JSONL rather than a store.

    A ``.jsonl`` suffix is unambiguous — an omnigraph store is a Lance
    *directory*, never a file — so this needs no flag. It matters because a
    JSONL export is the only *transportable* form of a store: Lance embeds
    absolute paths, so a ``.omni`` directory cannot be copied to another
    machine or streamed into a pod, while its export can (`kubectl exec -i …
    'cat > /tmp/x.jsonl'`). Merging from an export is what makes the
    local → deployed cutover executable at all.
    """
    return source.endswith(".jsonl")


def _check_export_source(source: str) -> None:
    """Fail with a message that points at the real problem for a bad export path.

    An export is bytes, not a store, so witan does not fetch one — the two ways
    a ``.jsonl`` source goes wrong need different answers, and the generic
    "no such file" sends a remote URI in the wrong direction entirely.
    """
    if source.startswith(("http://", "https://", "s3://")):
        raise RuntimeError(
            f"{source}: a `.jsonl` source is read as an `omnigraph export` "
            "file, and witan does not fetch remote ones — it has no "
            "credentials for them and an export is bytes, not a store. "
            "Download it with whatever already has access (e.g. `aws s3 cp "
            f"{source} ./export.jsonl`) and pass the local path."
        )
    if not Path(source).is_file():
        raise RuntimeError(
            f"{source}: no such export file. A `.jsonl` source is read as an "
            "`omnigraph export`, not a store — produce one with "
            f"`omnigraph export --store <store> > {source}`."
        )


def merge_store(
    source: str, *, target: str | None = None, dry_run: bool = False
) -> dict:
    """Merge another store's data into this store, newest-record-wins on slug
    collisions.

    Implements the runbook's export → reconcile → ``load --mode merge`` path
    (docs/migration-runbook.md): for every node that exists in both stores
    (matched on ``(type, slug)``), keeps whichever has the newer comparison
    timestamp (``_RECONCILE_TS_FIELDS``) instead of relying on
    ``omnigraph load --mode merge``'s raw last-loaded-wins overwrite, which
    ignores content entirely. Rows only in ``source`` are always added; rows
    only in the target are left untouched. Edge rows have no slug and are not
    reconciled — they pass through unchanged in the same load.

    Repeatable by construction: re-running against the same source and an
    already-merged target loads nothing new (every source row loses
    reconciliation to its own already-applied, equal-or-newer copy) — safe to
    run on a schedule or after every session rather than as a one-shot.

    Parameters
    ----------
    source:
        What to merge from: a store URI (local path, ``s3://``, ``file://``, or
        an ``http(s)://`` omnigraph-server), or the path to an
        ``omnigraph export`` JSONL — anything ending ``.jsonl`` is read as an
        export and not re-exported, and must be a readable *local* file (witan
        fetches no remote exports). The export form is the one that crosses
        machines: a Lance ``.omni`` directory embeds absolute paths and cannot
        be copied, so handing a store to another host (or into a cluster pod)
        means handing over its export.
    target:
        Store URI to merge into. Defaults to the configured store. Created
        (schema-applied, empty) automatically if it's a local path that
        doesn't exist yet — same as ``witan serve``/``witan <cmd>`` on a
        fresh machine; no-op for a remote ``s3://``/``http(s)://`` target,
        which is assumed to already exist. An ``http(s)://`` target names a
        deployed omnigraph-server and carries its graph id either from the
        configured ``WITAN_MEMORY_GRAPH`` (when it names the configured
        server) or inline as ``http://host:8080/graphs/<id>``; a remote target
        with neither is rejected. Unlike ``source``, a ``.jsonl`` target is
        refused rather than treated as a store — merging appends to a graph,
        and an export is a snapshot of one.
    dry_run:
        Compute and return the reconciliation decisions without loading
        anything.

    Returns counts (``added``/``updated``/``kept_target``) and the full
    per-``(type, slug)`` decision list, plus (when not a dry run)
    ``rows_loaded`` and the raw ``load`` output.
    """
    # `_acquire_store_lock`/`_ensure_graph` build filesystem `Path`s directly
    # from the URI and don't strip a URI scheme — same convention as the rest
    # of witan (`OmnigraphClient`, `_ensure_graph`), where a local store is
    # always a plain path and only http(s)/s3 count as "remote". `omnigraph`
    # itself accepts an explicit `file://` for `--store`, so callers may
    # reasonably pass one; strip it before it reaches any local Path logic.
    if source.startswith("file://"):
        source = source[len("file://") :]
    target = target or client.graph_uri
    if target.startswith("file://"):
        target = target[len("file://") :]
    from_export = _is_export_file(source)
    if from_export:
        _check_export_source(source)
    if _is_export_file(target):
        # `.jsonl` means "export" on the source side, and the asymmetry is a
        # trap: a missing local target is auto-created, so `--target x.jsonl`
        # would `omnigraph init` a Lance store *directory* named `x.jsonl` and
        # report a successful merge into a store nobody will ever read.
        raise RuntimeError(
            f"{target}: a target must be a store, not an `omnigraph export` "
            "file — merging appends to a graph, and an export is a snapshot of "
            "one. Pass the store to merge into (a local path, `s3://`, or "
            "`http(s)://<host>/graphs/<id>`)."
        )

    _ensure_graph(target)
    target_client = _store_client(target)

    # Held across export → reconcile → load, so no other writer can land
    # between the target export and the load and make the decisions stale. The
    # load inside re-enters this lock rather than blocking on it; a remote
    # target takes no lock at all (flock coordinates local writers only).
    with target_client.hold_write_lock():
        with tempfile.TemporaryDirectory(prefix="witan-merge-") as tmp:
            tmp_path = Path(tmp)
            target_file = tmp_path / "target.jsonl"

            if from_export:
                source_file = Path(source)
            else:
                source_file = tmp_path / "source.jsonl"
                _store_client(source).export_to(source_file, label="export (source)")
            target_client.export_to(target_file, label="export (target)")

            source_nodes, source_edges = _parse_export(source_file)
            target_nodes, _ = _parse_export(target_file)

            decisions, winners = _reconcile_nodes(source_nodes, target_nodes)
            counts = _decision_counts(decisions)

            if dry_run:
                return {
                    "dry_run": True,
                    "target": target,
                    "decisions": decisions,
                    **counts,
                }

            to_load = winners + source_edges
            if not to_load:
                return {
                    "merged": True,
                    "target": target,
                    "decisions": decisions,
                    "rows_loaded": 0,
                    **counts,
                }

            # CHUNKED, even though the target may be local. Until omnigraph
            # 0.9 the only ceiling on a load was the served request body, so a
            # local merge could safely go in one call — which is why this used
            # a bare `load_batch`. 0.9 added a per-table row cap enforced by
            # the engine itself, on local stores too, so a merge with more
            # than `LOAD_MAX_ROWS` winning rows of one type is now refused
            # here exactly as it would be over HTTP.
            #
            # `chunk_records` also emits every node before any edge, which
            # matters more here than the row bound does: `to_load` is
            # `winners + source_edges` and an edge whose endpoint lost
            # reconciliation resolves against the copy already in the target.
            #
            # ATOMICITY IS TRADED AWAY. Batches commit independently, so a
            # failure part-way leaves the earlier ones applied. That is
            # recoverable by re-running rather than by cleanup: reconciliation
            # makes a re-sent row lose to its own already-applied copy, which
            # is the same "repeatable by construction" property this function's
            # docstring already promises.
            outputs = [
                target_client.load_batch(batch, "merge")
                for batch in chunking.chunk_records(to_load)
            ]
            load_out = "\n".join(out.strip() for out in outputs if out.strip())

    return {
        "merged": True,
        "target": target,
        "decisions": decisions,
        "rows_loaded": len(to_load),
        "output": load_out.strip(),
        **counts,
    }


@contextmanager
def _data_tier_outage_reads_as_retryable():
    """Turn an exhausted-retry data-tier outage into advice the caller can use.

    ``store_merge``'s caller is a person migrating their own graph through the
    MCP tier. The data tier is ClusterIP-only: they cannot see it, cannot know
    it is restarting, and omnigraph's own error hands them a Rust backtrace
    naming an internal hostname they have no access to.

    The two things they can act on are that the outage is transient and that
    re-running is safe — batches reconcile against the target as it stands, so
    a re-sent row loses to its own already-applied copy. That is also why a
    part-way failure needs no cleanup, only a re-run.

    The original stays on ``__cause__`` for whoever reads the server logs.
    """
    try:
        yield
    except StoreUnavailable as exc:
        raise RuntimeError(
            "The witan deployment's data tier is temporarily unavailable (it "
            "did not come back within the restart window). Re-run the same "
            "`witan migrate merge` command once it is back — the merge is "
            "idempotent, so any batch that already landed is kept, not "
            "duplicated."
        ) from exc


@_tool
def store_merge(rows: list[dict], dry_run: bool = False) -> dict:
    """Merge a batch of exported rows into this deployment's graph, as you.

    The MCP-tier half of ``witan migrate merge`` (ADR-0007 D5). A client
    exports its own store, splits the rows into batches, and calls this once
    per batch; the server reconciles each batch against its own graph and
    writes the winners. That keeps the whole cutover inside the per-actor
    identity and Cedar model — the write is authorized as the calling user,
    not as ``svc-witan-admin``, which is the difference between this and the
    in-cluster path (ADR-0005 b).

    Every store operation here goes through the module-level ``client``, which
    re-resolves to *this request's* actor on each access. There is no service
    account behind it: an actor with no provisioned omnigraph token is refused
    rather than served under one, and a row type the caller's Cedar grant does
    not cover fails at the data tier.

    ``rows`` are ``omnigraph export`` records — ``{"type": Node, "data": {…}}``
    for a node, ``{"edge": Edge, "from": …, "to": …}`` for an edge — the shape
    ``merge_store``'s own export parsing produces. Nodes are reconciled
    newest-record-wins per ``(type, slug)`` against what this graph already
    holds, by the *same* ``_reconcile_nodes`` the in-process path uses. Edges
    carry no slug and pass through additively, exactly as they do there.

    Parameters
    ----------
    rows:
        One batch of ``omnigraph export`` records — ``{"type": Node, "data":
        {…}}`` for a node, ``{"edge": Edge, "from": …, "to": …}`` for an edge.
    dry_run:
        Reconcile and return the per-row ``decisions`` **without writing
        anything**. Run the whole migration this way first: it is the only way
        to see which side wins each ``(type, slug)`` before the graph changes.

    **Batching is the caller's job, and the caller must send every node before
    any edge** (``witan_core.chunking.chunk_records`` does both). Batches commit
    independently, so a failure part-way leaves earlier ones applied — which is
    safe to recover from by simply re-running, since reconciliation makes a
    re-sent row lose to its own already-applied copy. It is not, however,
    atomic, and a caller needing all-or-nothing has to arrange that itself.

    Returns this batch's per-row ``decisions`` plus ``added``/``updated``/
    ``kept_target`` counts, and ``rows_loaded``. With ``dry_run`` the decisions
    are computed and nothing is written.
    """
    if not rows:
        return {
            "decisions": [],
            "rows_loaded": 0,
            "dry_run": dry_run,
            **_decision_counts([]),
        }

    source_nodes, source_edges = _classify_rows(rows, "merge batch")

    with tempfile.TemporaryDirectory(prefix="witan-store-merge-") as tmp:
        target_file = Path(tmp) / "target.jsonl"
        with _data_tier_outage_reads_as_retryable():
            client.export_to(target_file, label="export (deployed graph)")
        target_nodes, _ = _parse_export(target_file)

    decisions, winners = _reconcile_nodes(source_nodes, target_nodes)
    counts = _decision_counts(decisions)

    if dry_run:
        return {"dry_run": True, "decisions": decisions, "rows_loaded": 0, **counts}

    to_load = winners + source_edges
    with _data_tier_outage_reads_as_retryable():
        client.load_batch(to_load, "merge")
    return {
        "dry_run": False,
        "decisions": decisions,
        "rows_loaded": len(to_load),
        **counts,
    }


# ── Composite re-rank (spec §7) ───────────────────────────────────


# `memory_search`'s documented result size. Applied by the caller *after*
# supersession pruning, never to the candidate set below: capping candidates
# first would let 20 superseded content hits discard every title hit and then
# prune to nothing, with a perfectly good title match sitting behind the cut.
_SEARCH_LIMIT = 20


def _search_rows(query: str, repo: str | None, kind: str | None) -> list[dict]:
    """BM25 candidate rows in score-desc order (the seed step for §3.5 / §8).

    Two BM25 runs — one over ``content``, one over ``title`` — unioned here
    rather than in the query, because the engine won't ``or`` two ``search``
    predicates in one match. Content hits come first and title-only hits are
    appended: the two runs' scores are not on a comparable scale, so there is
    no honest way to interleave them by score, and downstream ranking reads
    *position* rather than score anyway (``_rerank``'s ``norm_bm25`` proxy).

    That ordering is a *seeding* order, not a guarantee about the final result.
    It gives content matches the higher positional proxy, but the proxy is one
    weighted term in ``_score`` alongside recency, corroboration and
    confidence — so a well-corroborated title-only hit can finish above a
    marginal content hit, exactly as it can among content hits today.

    Returns up to 2× each query's ``limit 20``. Callers cap; see
    ``_SEARCH_LIMIT``.
    """
    detected = repo_module.detect(override=repo)
    if detected and kind:
        name = "search_by_repo_and_kind"
        params = {"query": query, "repo": detected, "kind": kind}
    elif detected:
        name = "search_by_repo"
        params = {"query": query, "repo": detected}
    elif kind:
        name = "search_by_kind"
        params = {"query": query, "kind": kind}
    else:
        name = "search_all"
        params = {"query": query}

    rows = list(client.read("read.gq", name, params))
    seen = {r["slug"] for r in rows}
    rows.extend(
        r
        for r in client.read("read.gq", f"{name}_title", params)
        if r["slug"] not in seen
    )
    return rows


# The edge index is a handful of global queries; cache it briefly so a burst of
# searches doesn't re-scan the graph each time. Keyed by store URI (test stores
# differ) and dropped on any local edge write, so it's never stale within a
# process; the TTL bounds staleness from other processes writing the shared store.
_EDGE_INDEX_TTL_SECONDS = 5.0
_edge_index_cache: dict | None = None
_edge_index_cache_key: tuple[str, float] | None = None


def _invalidate_edge_index() -> None:
    """Drop the cached edge index after a local edge write."""
    global _edge_index_cache, _edge_index_cache_key
    _edge_index_cache = None
    _edge_index_cache_key = None


def _edge_index() -> dict:
    """Tally support degrees and conflict/supersession sets across the graph.

    Runs a handful of global single-column edge queries and counts in Python:
    cheaper than per-candidate traversals and independent of result-set size.
    Bounded by edge count, mirroring ``all_superseded_slugs``. Cached (see above).
    """
    global _edge_index_cache, _edge_index_cache_key
    now = time.monotonic()
    if (
        _edge_index_cache is not None
        and _edge_index_cache_key is not None
        and _edge_index_cache_key[0] == client.graph_uri
        and now - _edge_index_cache_key[1] < _EDGE_INDEX_TTL_SECONDS
    ):
        return _edge_index_cache

    def slugs(query: str) -> list[str]:
        return [r["slug"] for r in client.read("read.gq", query, {})]

    # Corroboration counts supporting edges touching a memory: RelatedTo, AppliesTo,
    # Refines (all directions), plus provenance (Informed, SessionProduced).
    # Contradicts is NOT support — it drives the penalty instead.
    corroboration: Counter = Counter()
    for query in (
        "rel_edges_from",
        "rel_edges_to",
        "applies_edges_from",
        "applies_edges_to",
        "refines_edges_from",
        "refines_edges_to",
        "informed_edges_to",
        "produced_edges_to",
    ):
        corroboration.update(slugs(query))
    contradicted = set(slugs("contradicts_edges_from")) | set(
        slugs("contradicts_edges_to")
    )
    superseded = set(slugs("all_superseded_slugs"))
    result = {
        "corroboration": corroboration,
        "contradicted": contradicted,
        "superseded": superseded,
    }
    _edge_index_cache = result
    _edge_index_cache_key = (client.graph_uri, now)
    return result


def _age_days(ts: str | None, now: datetime) -> float:
    """Whole/fractional days between an ISO timestamp and ``now`` (≥ 0)."""
    if not ts:
        return 0.0
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def _score(
    *,
    norm_bm25: float,
    age_days: float,
    corroboration: int,
    confidence: float | None,
    is_superseded: bool,
    is_contradicted: bool,
    rank_cfg: "cfg_module.RankConfig",
) -> float:
    """The composite memory score (spec §7.2). Pure — no I/O."""
    recency = (
        math.exp(-age_days / rank_cfg.half_life_days)
        if rank_cfg.half_life_days > 0
        else 0.0
    )
    conf = rank_cfg.default_confidence if confidence is None else confidence
    return (
        rank_cfg.w_bm25 * norm_bm25
        + rank_cfg.w_recency * recency
        + rank_cfg.w_corrob * math.log1p(corroboration)
        + rank_cfg.w_conf * conf
        - rank_cfg.penalty_superseded * is_superseded
        - rank_cfg.penalty_contradicted * is_contradicted
    )


def _rerank(
    rows: list[dict],
    *,
    now: datetime,
    rank_cfg: "cfg_module.RankConfig",
    edge_index: dict,
) -> list[dict]:
    """Re-order a BM25 candidate set by the composite score (spec §7.2).

    The engine can't project the BM25 score, so rank position is the
    normalised-BM25 proxy (top hit → 1.0, last → 0.0). Stable on ties via the
    original index.
    """
    n = len(rows)
    corroboration = edge_index["corroboration"]
    contradicted = edge_index["contradicted"]
    superseded = edge_index["superseded"]
    scored = []
    for i, r in enumerate(rows):
        score = _score(
            norm_bm25=1.0 if n <= 1 else (n - 1 - i) / (n - 1),
            age_days=_age_days(r.get("updated_at") or r.get("created_at"), now),
            corroboration=corroboration.get(r["slug"], 0),
            confidence=r.get("confidence"),
            is_superseded=r["slug"] in superseded,
            is_contradicted=r["slug"] in contradicted,
            rank_cfg=rank_cfg,
        )
        scored.append((score, i, r))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [r for _, _, r in scored]


# ── Tools ─────────────────────────────────────────────────────────


@_tool
def memory_search(
    query: str,
    repo: str | None = None,
    kind: MemoryKind | None = None,
    include_superseded: bool = False,
) -> list[dict]:
    """
    Plain BM25 text search over memories (no graph expansion — for that use
    ``recall``).

    Returns the top-20 memories ranked by BM25 relevance. Superseded memories are
    hidden unless ``include_superseded=True``.

    Parameters
    ----------
    query:
        Free-text search query. Searched against ``content`` and ``title``.
        Content matches seed ahead of title-only matches, so they carry the
        higher relevance proxy — but final order is the composite score, which
        also weighs recency, corroboration and confidence.
    repo:
        Repo scoping — see instructions.
    kind:
        Optional filter: ``pattern``, ``project_fact``, ``lesson``,
        or ``agent_context``.
    include_superseded:
        When ``True``, keep memories that a newer memory ``Supersedes``. Default
        ``False`` drops them.
    """
    rows = _search_rows(query, repo, kind)

    if not rows:  # nothing to prune or re-rank — skip the edge-index scan
        return rows

    edge_index = _edge_index()
    if not include_superseded:
        rows = [r for r in rows if r["slug"] not in edge_index["superseded"]]
    # Cap here, after pruning — see _SEARCH_LIMIT.
    return _rerank(
        rows,
        now=datetime.now(timezone.utc),
        rank_cfg=rank_cfg,
        edge_index=edge_index,
    )[:_SEARCH_LIMIT]


@_tool
def memory_list(
    kind: MemoryKind | None = None,
    repo: str | None = None,
    language: str | None = None,
) -> list[dict]:
    """
    List memories (no search), optionally filtered by kind, repo, and/or language.

    Browse stored memories without a search query — e.g. all ``lesson`` or
    ``pattern`` memories. Ordered most-recent first. To load context prefer
    ``recall``; use this for a plain kind-scoped listing (e.g.
    ``memory_list(kind="project_fact")`` at session start, or
    ``memory_list(kind="pattern", language="python")`` before writing code).

    Parameters
    ----------
    kind:
        Optional filter: ``pattern``, ``project_fact``, ``lesson``, or
        ``agent_context``. Omit to list all kinds.
    repo:
        Repo scoping — see instructions. With no repo detected and none passed,
        returns slim records (slug, kind, title, tags — no content) for unscoped
        memories; ``memory_get`` a slug for its full content.
    language:
        Optional post-filter by ``language`` (e.g. ``python``); applies to the
        full-content results, not the slim unscoped listing.
    """
    detected = repo_module.detect(override=repo)

    def _by_language(rows: list[dict]) -> list[dict]:
        if not language:
            return rows
        return [
            r for r in rows if (r.get("language") or "").lower() == language.lower()
        ]

    if detected and kind:
        return _by_language(
            client.read(
                "read.gq",
                "list_memories_by_repo_kind",
                {"repo": detected, "kind": kind},
            )
        )
    if detected:
        return _by_language(
            client.read("read.gq", "list_memories_by_repo", {"repo": detected})
        )
    if repo == "":
        # Explicit all-repos opt-in — return full content.
        if kind:
            return _by_language(
                client.read("read.gq", "list_memories_by_kind", {"kind": kind})
            )
        return _by_language(client.read("read.gq", "list_memories", {}))
    # No repo detected and no explicit override: return slim records for
    # unscoped memories (repo=null) only. Caller can memory_get any slug it needs.
    # Use unbounded queries so repo-scoped memories don't push unscoped ones out
    # of the top-100 window before the Python filter runs.
    if kind:
        all_rows = client.read(
            "read.gq", "list_memories_by_kind_unbounded", {"kind": kind}
        )
    else:
        all_rows = client.read("read.gq", "list_memories_unbounded", {})
    unscoped = [r for r in all_rows if not r.get("repo")]
    return [_slim_memory(r) for r in unscoped]


def _update_memory(slug: str, changes: dict) -> dict | None:
    """Read a memory, merge ``changes`` over its mutable fields, write it back.

    ``update_memory`` replaces every field it is given, so a partial update MUST
    merge onto the current row — passing only the changed fields would blank the
    omitted ones. Same read-merge-write shape as :func:`_update_task` and as the
    ``migrate_repo_keys`` rewrite. Returns the updated node or ``None``.
    """
    rows = client.read("read.gq", "get_memory", {"slug": slug})
    if not rows:
        return None
    current = rows[0]
    merged = {
        "slug": slug,
        "title": changes.get("title", current.get("title")),
        "content": changes.get("content", current.get("content")),
        "repo": changes.get("repo", current.get("repo")),
        "language": changes.get("language", current.get("language")),
        "category": changes.get("category", current.get("category")),
        "severity": changes.get("severity", current.get("severity")),
        "tags": changes.get("tags", current.get("tags")),
        "symbol_refs": changes.get("symbol_refs", current.get("symbol_refs")),
        "confidence": changes.get("confidence", current.get("confidence")),
        "updated_at": now_iso(),
    }
    client.change("mutations.gq", "update_memory", merged)
    rows = client.read("read.gq", "get_memory", {"slug": slug})
    return rows[0] if rows else None


def _store_memory(
    kind: MemoryKind,
    title: str,
    content: str,
    *,
    repo: str | None = None,
    language: str | None = None,
    category: str | None = None,
    severity: Literal["info", "warning", "critical"] | None = None,
    tags: list[str] | None = None,
    symbol_refs: list[str] | None = None,
    confidence: float | None = None,
    session_slug: str | None = None,
) -> dict:
    """Create a Memory node — shared by memory_store and workflow_trace_mine."""
    if isinstance(tags, str):
        tags = [tags]
    if isinstance(symbol_refs, str):
        symbol_refs = [symbol_refs]
    now = now_iso()
    slug = _make_slug(kind, title)
    detected_repo = repo_module.detect(override=repo)

    # The whole memory — node, its topics, its edges — is ONE commit. Every
    # separate `mutate` is a separate Lance version, and a store accumulating a
    # version per row is what drove point reads to 167ms in the PR #180 spike.
    # Order matters: `insert Tagged` resolves its endpoints against the
    # statements ahead of it, so the Memory and each Topic must precede the edge
    # that references them.
    steps: list[tuple[str, str, dict]] = [
        (
            "mutations.gq",
            "insert_memory",
            {
                "slug": slug,
                "kind": kind,
                "title": title,
                "content": content,
                "repo": detected_repo,
                "language": language,
                "category": category,
                "severity": severity,
                "author": _current_author(),
                "tags": tags,
                "symbol_refs": symbol_refs,
                "confidence": confidence,
                "created_at": now,
                "updated_at": now,
            },
        )
    ]

    # Dual-write tags → Topic{kind:"topic"} + Tagged edge. The string list stays
    # the source of truth for old readers; the Topic graph is the new traversal
    # surface. Idempotent on the topic slug, so shared tags reuse one node. Skip
    # blank tags and dedup so neither drives redundant upsert/link calls.
    for tag in dict.fromkeys(t for t in (tags or []) if t.strip()):
        steps += _tag_memory_steps(slug, tag, "topic")

    # Provenance: record which session produced this memory (best-effort). An
    # explicitly-threaded handle wins — it is the only source a deployed replica
    # has, since MCP 2026-07-28 carries no session state and the replica shares
    # no filesystem with the agent. Local stdio falls back to the parked handle.
    # The engine validates edge endpoints, so a stale handle pointing at a
    # session that no longer exists in the store would raise. Since the batch
    # commits or fails WHOLE, that would now take the memory down with it — so
    # on failure the batch is retried without the provenance edge. Nothing
    # committed on the failed attempt, which is what makes the retry clean.
    active = session_slug or _active_session_slug()
    session_linked = False
    if active:
        try:
            client.change_many(
                [
                    *steps,
                    (
                        "mutations.gq",
                        "link_session_produced",
                        {"from": active, "to": slug},
                    ),
                ]
            )
            _invalidate_edge_index()  # SessionProduced feeds corroboration
            session_linked = True
        except RuntimeError:
            client.change_many(steps)
    else:
        client.change_many(steps)

    result = {
        "slug": slug,
        "kind": kind,
        "repo": detected_repo,
        "session_linked": session_linked,
    }
    # Surface the silent provenance gap: with no active session, the memory is
    # stored but not linked to the project's session history or completion trace.
    # A one-line nudge lets the agent react (call workflow_session_start) instead
    # of losing the link without noticing.
    if active is None:
        result["note"] = (
            "Stored without an active workflow session, so it is not linked to a "
            "project (no SessionProduced provenance). Call workflow_session_start "
            "first if this memory should roll up to the project's history, and "
            "pass the session_slug it returns."
        )
    return result


@_tool
async def memory_store(
    kind: MemoryKind,
    title: str,
    content: str,
    repo: str | None = None,
    language: str | None = None,
    category: str | None = None,
    severity: Literal["info", "warning", "critical"] | None = None,
    tags: list[str] | None = None,
    symbol_refs: list[str] | None = None,
    confidence: float | None = None,
    session_slug: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """
    Store a new memory in the shared graph.

    Prefer this over your private built-in/session memory for anything durable
    and team-shareable — patterns, project facts, lessons, decisions — so other
    agents and future sessions can find it. Returns the slug of the created node
    so callers can link to it.

    Parameters
    ----------
    kind:
        ``pattern``      — coding convention or reusable technique
        ``project_fact`` — structural fact about a repo/service
        ``lesson``       — a correction or cautionary finding
        ``agent_context``— information a future agent on this task should know
    title:
        Short, human-readable label. Used in listings and search.
    content:
        Full text of the memory. Be specific: include the what, why, and any
        examples. This is the primary search target.
    repo:
        Repo scoping — see instructions.
    language:
        Programming language (for ``pattern`` kind). e.g. ``python``, ``typescript``.
    category:
        Thematic category (for ``project_fact`` kind).
        e.g. ``architecture``, ``deployment``, ``testing``, ``dependencies``.
    severity:
        Importance level (for ``lesson`` kind).
        ``info`` | ``warning`` | ``critical``.
    tags:
        Optional list of free-form tags for grouping.
    symbol_refs:
        Optional code-graph symbol ids (``<repo>#<path/to/file.py>::<Name>``,
        from the witan-code tools' ``symbol_id`` field) this memory concerns,
        e.g. the function a lesson is about. Stored as a soft reference.
    confidence:
        Optional author/agent trust in this memory, 0.0–1.0. Feeds the search
        re-rank; omitted memories use the configured default.
    session_slug:
        The ``ws-`` handle returned by ``workflow_session_start``, recording
        which session produced this memory. Pass it whenever you have one: the
        protocol carries no session state, so against a deployed service this is
        the only way the ``SessionProduced`` provenance edge can be created.
        Omit it under a local stdio server, which finds the handle itself.
    """
    # When no repo is known (not passed, and detection finds none), offer to
    # scope it rather than silently persisting an unscoped node. Falls back to
    # None (today's behavior) under automation / an unsupported client.
    repo = await elicit.repo_or_detect(ctx, repo)
    return await _offload(
        _store_memory,
        kind,
        title,
        content,
        repo=repo,
        language=language,
        category=category,
        severity=severity,
        tags=tags,
        symbol_refs=symbol_refs,
        confidence=confidence,
        session_slug=session_slug,
    )


@_tool
def memory_get(slug: str, include_topics: bool = False) -> dict | None:
    """
    Retrieve a single memory by its slug.

    Returns the full node or ``null`` if not found.

    Parameters
    ----------
    slug:
        The ``pat-`` / ``pf-`` / ``les-`` / ``ctx-`` slug to retrieve.
    include_topics:
        When ``True``, attach a ``topics`` list of the Topic nodes this memory is
        tagged with (slug/name/kind).
    """
    rows = client.read("read.gq", "get_memory", {"slug": slug})
    if not rows:
        return None
    node = rows[0]
    if include_topics:
        node["topics"] = client.read("read.gq", "topics_for_memory", {"slug": slug})
    return node


@_tool
def memory_update(
    slug: str,
    title: str | None = None,
    content: str | None = None,
    repo: str | None = None,
    language: str | None = None,
    category: str | None = None,
    severity: Literal["info", "warning", "critical"] | None = None,
    tags: list[str] | None = None,
    symbol_refs: list[str] | None = None,
    confidence: float | None = None,
) -> dict | None:
    """
    Correct a memory's fields in place. Only non-null arguments are applied.

    This is the repair tool for a memory whose *content was always meant to be
    what you are about to write* — a wrong ``repo`` (so it never showed up in
    repo-scoped reads), a typo'd title, a missing tag. Returns the updated node,
    or ``null`` if no memory has that slug.

    Which tool to reach for:

    - a field is wrong → ``memory_update`` (this one)
    - the knowledge itself changed → ``memory_store`` the new one, then
      ``memory_link(kind="supersedes")``; the old one stays readable as history
    - it should never have existed (accidental duplicate, test write) →
      ``memory_delete``
    - it contains secret material → **rotate the credential.** Neither this tool
      nor ``memory_delete`` erases the old value from the graph's history.

    ``kind`` is deliberately not updatable: a memory that turns out to be a
    different kind is a different memory — store it and supersede this one.

    Parameters
    ----------
    slug:
        The ``pat-`` / ``pf-`` / ``les-`` / ``ctx-`` slug of the memory to
        correct.
    title:
        New short, human-readable label.
    content:
        New full text. Replaces the existing content rather than appending —
        and note that rewriting content here leaves no record that it changed.
        If the *knowledge* changed, store a new memory and supersede this one
        instead.
    repo:
        Canonical repo URI to (re)scope the memory to. Case-folded on write, as
        every other repo-key path does: correcting a mis-scoped memory is this
        tool's headline use, and a key that does not match what repo detection
        returns would just mis-scope it again, differently.
    language:
        Programming language (``pattern`` kind). e.g. ``python``, ``typescript``.
    category:
        Thematic category (``project_fact`` kind). e.g. ``architecture``,
        ``deployment``, ``testing``, ``dependencies``.
    severity:
        Importance (``lesson`` kind): ``info`` | ``warning`` | ``critical``.
    tags:
        Free-form tags. Replaces the existing list. Each tag is also promoted to
        a ``Topic`` and linked. **Tags removed here keep their ``Tagged``
        edge** — edges cannot be individually retracted, and deleting the Topic
        to drop one would take out every other memory's edge to it. The string
        list stays authoritative for what the memory claims to be tagged with.
    symbol_refs:
        Code-graph symbol ids (``repo#path::Name``) this memory concerns.
        Replaces the existing list.
    confidence:
        Author/agent trust in this memory, 0.0–1.0. Feeds the recall re-rank.
    """
    if isinstance(tags, str):
        tags = [tags]
    if isinstance(symbol_refs, str):
        symbol_refs = [symbol_refs]
    changes = {
        key: value
        for key, value in (
            ("title", title),
            ("content", content),
            # Case-fold, as every other repo-key write path does. Correcting a
            # mis-scoped memory is the headline use of this tool, and a repo key
            # that doesn't match what repo.detect() returns just mis-scopes it
            # again, differently.
            ("repo", normalise(repo) if repo else repo),
            ("language", language),
            ("category", category),
            ("severity", severity),
            ("tags", tags),
            ("symbol_refs", symbol_refs),
            ("confidence", confidence),
        )
        if value is not None
    }
    updated = _update_memory(slug, changes)
    if updated is None:
        return None

    # Keep the Topic traversal surface in step with the string list, same
    # dual-write as _store_memory. Tags *removed* here keep their Tagged edge:
    # edges cannot be individually retracted, and deleting the Topic to drop one
    # would take out every other memory's edge to it. The string list stays
    # authoritative for what the memory claims to be tagged with.
    steps: list[tuple[str, str, dict]] = []
    for tag in dict.fromkeys(t for t in (tags or []) if t.strip()):
        steps += _tag_memory_steps(slug, tag, "topic")
    client.change_many(steps)
    return updated


@_tool
def memory_delete(slug: str, confirm: bool = False) -> dict:
    """
    Hard-delete a memory. Graph hygiene only — NOT a way to erase secrets.

    Use when a memory should never have existed: an accidental duplicate, a test
    write, a node created against the wrong graph. For anything else prefer
    ``memory_update`` (a field is wrong) or ``memory_store`` +
    ``memory_link(kind="supersedes")`` (the knowledge changed) — superseding is
    the soft delete, and it keeps the history legible.

    **This does not erase content.** The row remains fully readable, content
    included, from any prior commit of the graph. If a memory captured a
    credential, the fix is to **rotate the credential**; scrubbing history is an
    admin ``omnigraph cleanup``, which no MCP tool performs.

    Deleting a node also removes its incident edges in both directions, so a
    deleted memory leaves no dangling ``Supersedes``/``RelatedTo``/``Tagged``
    behind. Topic nodes on the far end of a ``Tagged`` edge survive and may be
    left with no memories.

    That is not the only way to remove an edge — edges are deletable on their
    own (``task_unlink``, ``_unlink_edge``). Deleting the node is still the
    only route for a *memory* edge, since no unlink tool covers those yet.

    Parameters
    ----------
    slug:
        The memory to delete permanently.
    confirm:
        Must be ``True``. Without it this is a no-op returning
        ``{"deleted": False, "reason": ...}``.

    Returns the deleted node's full fields under ``memory`` so an accidental
    delete can be re-stored straight from this result. Refuses (again as a
    no-op) when the caller is not the memory's author.
    """
    rows = client.read("read.gq", "get_memory", {"slug": slug})
    if not rows:
        return {"slug": slug, "deleted": False, "reason": "no such memory"}
    node = rows[0]

    if not confirm:
        return {
            "slug": slug,
            "deleted": False,
            "reason": "pass confirm=True to delete; this cannot be undone from "
            "the tool surface",
            "memory": node,
        }

    author = _current_author()
    if node.get("author") != author:
        return {
            "slug": slug,
            "deleted": False,
            "reason": f"authored by {node.get('author')!r}, not {author!r}; only "
            "the author can delete a memory",
        }

    client.change("mutations.gq", "delete_memory", {"slug": slug})
    return {"slug": slug, "deleted": True, "memory": node}


@_tool
async def memory_link(
    from_slug: str, to_slug: str, kind: MemoryLinkKind, ctx: Context | None = None
) -> dict:
    """
    Create a typed edge between two memories.

    - ``supersedes``  — ``from`` (newer) replaces ``to`` (older). ``to`` is hidden
                        from default ``memory_search`` results.
    - ``refines``     — ``from`` sharpens/extends ``to`` without replacing it.
    - ``applies_to``  — ``from`` (a pattern/lesson) applies in the context of ``to``
                        (a project_fact).
    - ``contradicts`` — ``from`` and ``to`` conflict. Symmetric; surfaced for
                        review, never hidden.
    - ``related_to``  — soft association. Symmetric.
    - ``tagged``      — ``from`` (a Memory) is about ``to`` (a Topic). ``to`` is
                        either an existing Topic slug (``tp-...``) or a ``name:kind``
                        spec (e.g. ``cryptography:topic``, ``DATABASE_URL:contract``),
                        in which case the Topic is auto-created.

    For memory↔memory kinds both endpoints must already exist as ``Memory`` nodes;
    the edge is not written otherwise (avoids dead off-type edges). A memory cannot
    link to itself. Returns ``linked: False`` in those cases rather than raising.

    Parameters
    ----------
    from_slug:
        The memory the edge points **from**. Direction is load-bearing for the
        asymmetric kinds — for ``supersedes`` this is the *newer* memory.
    to_slug:
        The memory the edge points **to** — for ``supersedes``, the older memory
        being replaced. For ``kind="tagged"`` this is a Topic instead: either an
        existing ``tp-`` slug or a ``name:kind`` spec, which auto-creates it.
    kind:
        Which edge to write: ``supersedes`` | ``refines`` | ``applies_to`` |
        ``contradicts`` | ``related_to`` | ``tagged``. See the descriptions
        above — ``supersedes`` is the one that changes what default reads
        return.
    """
    if from_slug == to_slug:
        return {
            "from": from_slug,
            "to": to_slug,
            "kind": kind,
            "linked": False,
            "reason": "cannot link a memory to itself",
        }

    if kind == "tagged":
        if not await _offload(
            client.read, "read.gq", "get_memory", {"slug": from_slug}
        ):
            return {
                "from": from_slug,
                "to": to_slug,
                "kind": kind,
                "linked": False,
                "missing": [from_slug],
            }
        topic_slug, topic_steps = await _offload(_resolve_topic_steps, to_slug)
        if topic_slug is None:
            return {
                "from": from_slug,
                "to": to_slug,
                "kind": kind,
                "linked": False,
                "missing": [to_slug],
            }
        await _offload(
            client.change_many,
            [
                *topic_steps,
                ("mutations.gq", "link_tagged", {"from": from_slug, "to": topic_slug}),
            ],
        )
        return {"from": from_slug, "to": topic_slug, "kind": kind, "linked": True}

    endpoints = {from_slug, to_slug}
    present = {
        slug
        for slug in endpoints
        if await _offload(client.read, "read.gq", "get_memory", {"slug": slug})
    }
    missing = sorted(endpoints - present)
    if missing:
        return {
            "from": from_slug,
            "to": to_slug,
            "kind": kind,
            "linked": False,
            "missing": missing,
        }

    # ``supersedes`` hides the older memory from default search — confirm that
    # loss of visibility. Headless/unsupported clients proceed as before; only an
    # explicit decline aborts. Other kinds don't hide anything, so no prompt.
    if kind == "supersedes" and not await elicit.confirm(
        ctx,
        f"Superseding hides {to_slug} from default memory_search results "
        f"(in favor of {from_slug}). Proceed?",
        default_when_unsupported=True,
        title="Supersede?",
    ):
        return {
            "from": from_slug,
            "to": to_slug,
            "kind": kind,
            "linked": False,
            "reason": "declined",
        }

    await _offload(
        client.change,
        "mutations.gq",
        _MEMORY_LINK_MUTATIONS[kind],
        {"from": from_slug, "to": to_slug},
    )
    _invalidate_edge_index()  # supersede/contradict/support sets changed
    return {"from": from_slug, "to": to_slug, "kind": kind, "linked": True}


@_tool
def memory_neighbors(slug: str, kinds: list[MemoryLinkKind] | None = None) -> dict:
    """
    Return the memories directly linked to ``slug``, grouped by edge kind.

    For symmetric kinds (``contradicts``, ``related_to``) both directions are
    unioned and de-duplicated. Use after ``memory_get`` to see what a memory
    connects to.

    Parameters
    ----------
    slug:
        The memory whose neighbours to fetch.
    kinds:
        Optional subset of edge kinds to include. Omit (``None``) for all kinds;
        an explicit empty list returns no kinds.
    """
    wanted = list(_MEMORY_NEIGHBOR_QUERIES) if kinds is None else kinds
    neighbors: dict[str, list[dict]] = {}
    for kind in wanted:
        if (
            kind not in _MEMORY_NEIGHBOR_QUERIES
        ):  # e.g. "tagged" → use memory_get/topic_get
            continue
        merged: dict[str, dict] = {}
        for query_name in _MEMORY_NEIGHBOR_QUERIES[kind]:
            for row in client.read("read.gq", query_name, {"slug": slug}):
                merged[row["slug"]] = row
        neighbors[kind] = list(merged.values())
    return {"slug": slug, "neighbors": neighbors}


@_tool
def topic_get(topic: str) -> dict | None:
    """
    Resolve a Topic and return it with the memories tagged to it.

    ``topic`` is either a Topic slug (``tp-...``) or a ``name:kind`` spec
    (e.g. ``uv:topic``). Because topics are a cross-repo join surface, the
    returned memories may span repositories — this is the traversal-based
    retrieval primitive: two memories in different repos sharing a topic are
    one hop apart.

    Returns ``{"topic": {...}, "memories": [...]}`` or ``null`` if no such Topic.

    Parameters
    ----------
    topic:
        Either a Topic slug (``tp-...``) or a ``name:kind`` spec (``uv:topic``,
        ``DATABASE_URL:contract``). A spec resolves through the same
        deterministic slugify used on write rather than an exact name match, so
        a tag stored as ``UV`` is still found by ``uv:topic``.
    """
    # A tp- slug hits get_topic directly. A name:kind spec resolves through the
    # deterministic _topic_slug (NOT an exact name match) so that casing/whitespace
    # differences — stored tag "UV" queried as "uv:topic" — still find tp-topic-uv.
    if topic.startswith("tp-"):
        slug = topic
    else:
        name, sep, kind = topic.rpartition(":")
        if not (sep and kind in ("topic", "contract", "symbol", "entity") and name):
            return None
        slug = _topic_slug(kind, name)
    topic_rows = client.read("read.gq", "get_topic", {"slug": slug})
    if not topic_rows:
        return None
    memories = client.read("read.gq", "memories_for_topic", {"topic_slug": slug})
    return {"topic": topic_rows[0], "memories": memories}


# ── Code Branch Tracking ───────────────────────────────────────────
#
# Links a git branch (repo + raw branch name) to the task/project it's
# carrying — schema.pg § Code Branches. Wired into workflow_session_start
# and task_claim below; both auto-detect the current checkout's repo+branch
# and no-op silently when neither is available (no git context, detached
# HEAD) — this is best-effort coordination metadata, never a hard
# requirement for the tool it's attached to.


def _code_branch_slug(repo: str, branch: str) -> str:
    return f"{repo}|{branch}"


def _upsert_code_branch_step(repo: str, branch: str) -> _Step:
    """The mutation that creates or touches the CodeBranch for (repo, branch).

    A fresh branch is inserted with status ``active``; an existing one is
    just touched (``updated_at`` bumped, status reset to ``active`` — a
    branch resuming work after being marked ``abandoned`` is active again).

    Returns the step rather than issuing it so the caller can commit it
    together with whatever else its tool call is writing — see
    :func:`_code_branch_steps`. The read that decides insert-vs-touch still
    happens here; only the write is deferred.
    """
    slug = _code_branch_slug(repo, branch)
    now = now_iso()
    if client.read("read.gq", "get_code_branch", {"slug": slug}):
        return (
            "mutations.gq",
            "touch_code_branch",
            {"slug": slug, "status": "active", "updated_at": now},
        )
    return (
        "mutations.gq",
        "insert_code_branch",
        {
            "slug": slug,
            "repo": repo,
            "branch": branch,
            "status": "active",
            "created_at": now,
            "updated_at": now,
        },
    )


def _works_on_step(branch_slug: str, task_slug: str) -> _Step | None:
    """Idempotent WorksOn edge — task_claim re-calls (lease renewal) must not
    pile up duplicate edges between the same branch and task. ``None`` when the
    edge already exists."""
    if client.read(
        "read.gq",
        "code_branch_works_on_edge",
        {"branch_slug": branch_slug, "task_slug": task_slug},
    ):
        return None
    return ("mutations.gq", "link_works_on", {"from": branch_slug, "to": task_slug})


def _for_project_step(branch_slug: str, project_slug: str) -> _Step | None:
    """Idempotent ForProject edge — see :func:`_works_on_step`."""
    if client.read(
        "read.gq",
        "code_branch_for_project_edge",
        {"branch_slug": branch_slug, "project_slug": project_slug},
    ):
        return None
    return (
        "mutations.gq",
        "link_for_project",
        {"from": branch_slug, "to": project_slug},
    )


def _code_branch_steps(
    repo: str | None, *, task_slug: str | None = None, project_slug: str | None = None
) -> list[_Step]:
    """Mutations tracking the current checkout's CodeBranch, for a caller's batch.

    Returns ``[]`` in exactly one situation: there is no checkout to describe —
    no repo context, no current git branch (detached HEAD, outside a repo), or
    construction failed (see best-effort below).

    ★ "ALREADY RECORDED" IS NOT ONE OF THEM. With a repo and a branch the list
    is never empty: `_upsert_code_branch_step` yields an insert for a new
    branch and a ``touch_code_branch`` for one already present, and that touch
    is not redundant — bumping ``updated_at`` is what tells the branch reaper
    (ADR-0006) the branch is still live. Only the EDGE steps drop out when the
    edge already exists. A caller that reads an empty-on-no-op contract here
    and optimizes the touch away silently makes live branches look abandoned;
    `test_re_entrant_session_start_costs_one_commit` pins the touch for that
    reason.

    ★ BEST-EFFORT IS PRESERVED, AND ITS SCOPE IS NOW EXACT. This is metadata
    riding alongside a task/workflow tool call, not the tool's purpose, so it
    must never be what fails that call. What can fail here is CONSTRUCTION —
    shelling out to git for the branch name, and the reads that decide
    insert-vs-touch and whether an edge already exists — and all of it is
    caught. The returned steps then commit inside the CALLER's mutation, where
    a failure is the caller's own write failing and is rightly fatal: the
    alternative is a tool that reports success having written nothing.

    That split is the whole point of returning steps. Issuing these separately
    cost `workflow_session_start` up to three extra Lance commits — at ~3.5-4s
    each against the deployed store, against a 30s deadline for the whole tool
    call (tk-batch-the-hot-witan-write-paths-one-tool-call-is-a8227e).
    """
    if not repo:
        return []
    branch = repo_module.current_branch()
    if not branch:
        return []
    try:
        steps = [_upsert_code_branch_step(repo, branch)]
        branch_slug = _code_branch_slug(repo, branch)
        # Edges may reference a node inserted earlier in the SAME mutation
        # body — endpoint validation resolves against the in-flight statements
        # — so the CodeBranch and its edges legitimately share one commit.
        if task_slug and (step := _works_on_step(branch_slug, task_slug)):
            steps.append(step)
        if project_slug and (step := _for_project_step(branch_slug, project_slug)):
            steps.append(step)
    except Exception as exc:  # noqa: BLE001 — coordination metadata, never fatal
        logger.warning(
            "witan.code_branch.tracking_failed",
            repo=repo,
            branch=branch,
            task=task_slug,
            project=project_slug,
            error=str(exc),
            exc_info=True,
        )
        return []
    return steps


def _track_code_branch(
    repo: str | None, *, task_slug: str | None = None, project_slug: str | None = None
) -> None:
    """Issue :func:`_code_branch_steps` as one standalone commit.

    For callers that cannot fold the steps into their own write. ``task_claim``
    is the case: its claim is a compare-and-swap with a post-write verification
    read, and the branch metadata must only be recorded once the claim is known
    won — so it cannot ride along, and retrying the claim must not re-apply it.

    Still never raises, for the same reason the step builder doesn't: this is
    metadata beside the tool's purpose. One commit now instead of up to three.
    """
    steps = _code_branch_steps(repo, task_slug=task_slug, project_slug=project_slug)
    if not steps:
        return
    try:
        client.change_many(steps)
    except Exception as exc:  # noqa: BLE001 — coordination metadata, never fatal
        logger.warning(
            "witan.code_branch.tracking_failed",
            repo=repo,
            task=task_slug,
            project=project_slug,
            error=str(exc),
            exc_info=True,
        )


# ── Workflow Tracking Tools ───────────────────────────────────────

WorkflowPhase = Literal["discovery", "spec", "implementation", "delivery"]
WorkflowStatus = Literal["active", "completed", "abandoned"]

_PHASE_ORDER = {"discovery": 0, "spec": 1, "implementation": 2, "delivery": 3}

# Below this, a completion outcome is "thin" enough to offer to expand it before
# it's sealed into the immutable corpus trace (workflow_project_complete).
_THIN_OUTCOME_CHARS = 40


def _advance_advisory(prev_phase: str | None, new_phase: str) -> str | None:
    """A soft, non-blocking note when a phase transition is unusual.

    Advancing is intentionally unconstrained (backward, skip-ahead, and
    complete-from-discovery are all permitted), but an unusual transition is
    indistinguishable from a mistake without a signal. Returns a one-line
    advisory for a backward or skip-ahead move (or a no-op re-advance), else
    ``None`` for the normal forward-by-one step.
    """
    if prev_phase == new_phase:
        return f"Already in the '{new_phase}' phase — no change."
    prev_i = _PHASE_ORDER.get(prev_phase or "")
    new_i = _PHASE_ORDER.get(new_phase)
    if prev_i is None or new_i is None:
        return None
    if new_i < prev_i:
        return (
            f"Moved backward ({prev_phase} → {new_phase}). Allowed, but unusual — "
            "confirm this is intentional."
        )
    if new_i > prev_i + 1:
        skipped = sorted(
            (ph for ph, i in _PHASE_ORDER.items() if prev_i < i < new_i),
            key=lambda ph: _PHASE_ORDER[ph],
        )
        return (
            f"Skipped ahead ({prev_phase} → {new_phase}), bypassing "
            f"{', '.join(skipped)}. Allowed, but unusual."
        )
    return None


def _is_local_stdio() -> bool:
    """True when this process is the user's own stdio server, not a deployment.

    Only then does the server share a filesystem (and a ``$CLAUDE_SESSION_ID``)
    with the agent whose Stop hook reads the session handle. ``oidc_issuer`` is
    the same discriminator ``_resolve_client`` uses for per-user isolation.
    """
    return not identity_cfg.oidc_issuer


def _active_session_slug() -> str | None:
    """The WorkflowSession slug for the current agent session, or None.

    Reads the handle stored locally under ``$CLAUDE_SESSION_ID``. Fails soft on
    any missing-env/read/parse error — provenance is best-effort and must never
    block a memory write.

    Local-stdio only: a deployed replica shares neither the filesystem nor the
    agent's session id, so there is nothing to read. Under a deployment the
    handle instead arrives as the caller's ``session_slug`` argument (injected by
    ``RemoteMCPProxy._resolve_session_slug`` for CLI call sites), which is why
    this is the *fallback* in ``_store_memory`` rather than its only source.
    """
    if not _is_local_stdio():
        return None
    handle = session_state.read_handle(os.environ.get("CLAUDE_SESSION_ID") or "")
    return (handle or {}).get("session_slug") or None


@_tool
def workflow_project_create(
    title: str,
    description: str,
    phase: WorkflowPhase = "discovery",
    repos: list[str] | None = None,
    github_issue: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """
    Create a new workflow project to track an engineering objective.

    Call this at the start of a multi-session project before calling
    ``workflow_session_start``. The returned slug is used to link sessions.

    A project may span several repos (a service that touches a Django app, its
    frontend, and the infra repo that deploys it) or none (a cross-cutting
    objective not yet tied to any repo). The repo set also grows automatically
    as sessions run in new repos — see ``workflow_session_start``.

    Parameters
    ----------
    title:
        Short name for the project. Used in listings and injected context.
    description:
        Full description of the objective — what will be built or changed and why.
    phase:
        Starting phase. One of ``discovery``, ``spec``, ``implementation``,
        ``delivery``. Defaults to ``discovery``.
    repos:
        Canonical repo URIs this project spans. The current repo (detected from
        ``.git/config``) is added automatically. Omit entirely when creating a
        repo-less "floating" project from outside any git repo. Guessing here is
        fine — a project's real blast radius is rarely known at creation. The
        set can be corrected at any time with ``workflow_project_update``
        (``repos`` to replace it, ``add_repos``/``remove_repos`` to nudge it),
        and it also grows on its own as sessions run in new repos.
    github_issue:
        URL of the GitHub issue tracking this work.
        e.g. ``github.com/mitodl/ol-django/issues/847``.
    tags:
        Optional list of tags for grouping and searching.
    """
    now = now_iso()
    slug = _make_slug("workflow_project", title)
    repo_set = _merge_repos(repos, repo_module.detect())

    client.change(
        "mutations.gq",
        "insert_workflow_project",
        {
            "slug": slug,
            "title": title,
            "description": description,
            "repos": repo_set or None,
            "status": "active",
            "phase": phase,
            "author": _current_author(),
            "tags": tags,
            "github_issue": github_issue,
            "github_pr": None,
            "created_at": now,
            "updated_at": now,
        },
    )
    return {"slug": slug, "repos": repo_set, "phase": phase}


@_tool
def workflow_project_get(slug: str) -> dict | None:
    """
    Retrieve a single workflow project by slug.

    Returns the full project node (including ``blocked_by`` and ``blocks``
    lists) or ``null`` if not found.

    ``blocked_by`` lists the ``wp-`` slugs of projects that must complete
    before this one is ready. ``blocks`` lists projects this project is
    currently blocking (derived by scanning all active projects).

    Parameters
    ----------
    slug:
        The ``wp-`` slug to retrieve.
    """
    rows = client.read("read.gq", "get_workflow_project", {"slug": slug})
    if not rows:
        return None
    project = rows[0]

    # Compute the `blocks` list: active projects whose blocked_by includes this slug.
    all_rows = client.read("read.gq", "list_projects_by_status", {"status": "active"})
    blocks = [r["slug"] for r in all_rows if slug in (r.get("blocked_by") or [])]
    project["blocks"] = blocks
    return project


def _live_sessions(rows: list[dict]) -> list[dict]:
    """Drop sessions a retry minted, which ``migrate dedupe-sessions`` flagged.

    Every aggregate over sessions — trace assembly, resume summary, per-phase
    staleness counts — runs through this, so a duplicate that predates the
    ``workflow_session_start`` upsert can't inflate the corpus. The engine has
    no where-clause, hence a Python filter over the full listing rather than a
    query-level one.
    """
    return [r for r in rows if not r.get("superseded_by")]


def _project_sessions(project_slug: str) -> list[dict]:
    """A project's real sessions, ordered ``started_at`` asc."""
    return _live_sessions(
        client.read(
            "read.gq", "list_sessions_by_project", {"project_slug": project_slug}
        )
    )


def _latest_session_summary(project_slug: str) -> dict | None:
    """The most-recently-started session for a project, condensed for resume.

    ``list_sessions_by_project`` returns sessions ordered by ``started_at`` asc,
    so the last row is the latest. Returns ``None`` when the project has no
    sessions yet. Shared by ``workflow_project_status`` and the context hook so
    "where things stand" reads the same everywhere.
    """
    sessions = _project_sessions(project_slug)
    if not sessions:
        return None
    latest = sessions[-1]
    return {
        "slug": latest.get("slug"),
        "phase": latest.get("phase"),
        "summary": latest.get("summary"),
        "ended_at": latest.get("ended_at"),
        "open": not latest.get("ended_at"),
    }


@_tool
def workflow_project_status(slug: str) -> dict | None:
    """One-call "what should I do next" resume view for a workflow project.

    Combines the four things an agent (or the context hook) needs to pick up a
    project without re-deriving state: current **phase**, the **ready tasks**
    under it (same readiness rule as ``task_ready``), the **last session's
    handoff summary** (and whether it's still open), and any project-level
    **blockers**. Returns ``None`` if the project doesn't exist.

    Parameters
    ----------
    slug:
        The ``wp-`` slug of the project to resume.
    """
    rows = client.read("read.gq", "get_workflow_project", {"slug": slug})
    if not rows:
        return None
    p = rows[0]

    tasks = client.read("read.gq", "list_tasks_by_project", {"project_slug": slug})
    open_tasks = sum(1 for t in tasks if t.get("status") != "closed")
    # Delegate to task_ready (not readiness.filter_ready) so the "same rule as
    # task_ready" promise is literal: it fetches out-of-project blockers instead
    # of treating a blocker absent from this project's task set as closed.
    ready = task_ready(project_slug=slug, limit=100)

    return {
        "project": {
            k: p.get(k)
            for k in ("slug", "title", "phase", "status", "repos", "github_pr")
        },
        "ready_tasks": [
            {k: t.get(k) for k in ("slug", "title", "priority", "status", "assignee")}
            for t in ready
        ],
        "last_session": _latest_session_summary(slug),
        "blockers": list(p.get("blocked_by") or []),
        "counts": {"ready": len(ready), "open_tasks": open_tasks},
    }


@_tool
def workflow_project_list(
    repo: str | None = None,
    status: WorkflowStatus | None = "active",
    phase: WorkflowPhase | None = None,
    ready: bool = False,
) -> list[dict]:
    """
    List workflow projects, optionally filtered by repo, status, and phase.

    Defaults to listing only ``active`` projects. Pass ``status=None`` to see
    all statuses. The ``UserPromptSubmit`` hook calls this to inject project
    context into new sessions.

    Parameters
    ----------
    repo:
        Canonical repo URI to filter to (membership test against each
        project's repo set). Auto-detected from ``.git/config`` if omitted.
        Pass an empty string to list projects across all repos.
    status:
        ``active`` | ``completed`` | ``abandoned`` | ``None`` for all.
        Defaults to ``active``.
    phase:
        Optional phase filter applied after fetching.
    ready:
        When ``True``, only return active projects whose blockers are all
        completed (i.e. projects that are unblocked and actionable).
    """
    detected_repo = repo_module.detect(override=repo)

    # `repos` is a list, so membership can't be a graph match-filter — fetch by
    # status (or all) and filter on set membership in Python.
    if status:
        rows = client.read("read.gq", "list_projects_by_status", {"status": status})
    else:
        rows = client.read("read.gq", "list_all_projects", {})

    if detected_repo:
        rows = [r for r in rows if detected_repo in _project_repos(r)]

    if phase:
        rows = [r for r in rows if r.get("phase") == phase]

    if ready:
        # Ensure we only consider active projects — ready=True implies active scope.
        if not status:
            rows = [r for r in rows if r.get("status", "active") == "active"]
        status_cache: dict[str, str] = {
            r["slug"]: r.get("status", "active") for r in rows
        }
        rows = [r for r in rows if _project_is_ready(r, status_cache)]

    return rows


@_tool
def workflow_project_update(
    slug: str,
    title: str | None = None,
    description: str | None = None,
    repos: list[str] | None = None,
    add_repos: list[str] | None = None,
    remove_repos: list[str] | None = None,
    tags: list[str] | None = None,
    github_issue: str | None = None,
    status: Literal["active", "abandoned"] | None = None,
) -> dict | None:
    """
    Correct a project's metadata after creation.

    The escape hatch for everything ``workflow_project_advance`` (phase,
    ``github_pr``), ``workflow_project_complete`` (completion) and
    ``workflow_project_block``/``_unblock`` (dependencies) don't cover. Every
    parameter is optional and only what you pass is touched; omitting a field
    leaves it exactly as it was, so this can never blank something by accident.
    Returns the updated project, or ``None`` if the slug doesn't exist.

    The common case is repos. A project's real blast radius is rarely known
    during discovery, and until the set is right, repo-scoped recall from the
    repos where the work actually lands won't surface the project at all. Pass
    ``repos`` to replace the set wholesale, or ``add_repos``/``remove_repos`` to
    nudge it (both may be passed together; removals are applied after
    additions). Repos are a plain list field on the project node, not edges, so
    a removal really removes — unlike ``workflow_project_unblock``, which can
    only update its denormalized field because omnigraph edges are append-only.

    Two things this deliberately can't do:

    - **Set the phase.** ``workflow_project_advance`` stays the only route, so
      a transition is always seen by its ordering check. It does allow going
      backwards (with a confirmation), which is how a phase set in error gets
      corrected — this tool would just bypass the prompt.
    - **Complete a project.** ``status`` accepts ``abandoned`` (for work that
      stopped without an outcome) and ``active`` (to revive it), but not
      ``completed``: that belongs to ``workflow_project_complete``, which seals
      a corpus trace. Nothing should mint a trace without a narrative.

    Parameters
    ----------
    slug:
        The ``wp-`` slug of the project to update.
    title:
        Replacement short name.
    description:
        Replacement description.
    repos:
        Replace the repo set wholesale with these canonical URIs.
    add_repos:
        Canonical repo URIs to add to the set.
    remove_repos:
        Canonical repo URIs to drop from the set.
    tags:
        Replacement tag list. Pass ``[]`` to clear.
    github_issue:
        URL of the GitHub issue tracking this work.
    status:
        ``active`` | ``abandoned``.
    """
    rows = client.read("read.gq", "get_workflow_project", {"slug": slug})
    if not rows:
        return None
    current = rows[0]

    # Canonicalize every caller-supplied repo the same way `repo.detect` does,
    # so a differently-cased spelling of a repo already in the set updates that
    # entry instead of adding a near-duplicate beside it — and so `remove_repos`
    # matches what's actually stored.
    def _canon(values: list[str] | None) -> list[str]:
        return _merge_repos([normalise(r) for r in values or [] if r])

    base = _canon(repos) if repos is not None else _project_repos(current)
    dropped = set(_canon(remove_repos))
    new_repos = [r for r in _merge_repos(base, _canon(add_repos)) if r not in dropped]

    payload = {
        "slug": slug,
        "title": title if title is not None else current.get("title"),
        "description": (
            description if description is not None else current.get("description")
        ),
        "repos": new_repos or None,
        "status": status if status is not None else current.get("status", "active"),
        "tags": (tags if tags is not None else current.get("tags")) or None,
        "github_issue": (
            github_issue if github_issue is not None else current.get("github_issue")
        ),
        "updated_at": now_iso(),
    }
    client.change("mutations.gq", "update_workflow_project_fields", payload)

    updated = client.read("read.gq", "get_workflow_project", {"slug": slug})
    return updated[0] if updated else payload


@_tool
async def workflow_project_advance(
    slug: str,
    phase: WorkflowPhase,
    github_pr: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """
    Advance a workflow project to the next phase.

    Call when transitioning from e.g. spec to implementation. Optionally
    record a PR URL when moving to or through the delivery phase.

    Parameters
    ----------
    slug:
        The ``wp-`` slug of the project to update.
    phase:
        New phase: ``discovery`` | ``spec`` | ``implementation`` | ``delivery``.
    github_pr:
        URL of the GitHub PR if one has been opened.
    """
    now = now_iso()
    before = await _offload(
        client.read, "read.gq", "get_workflow_project", {"slug": slug}
    )
    prev_phase = before[0].get("phase") if before else None
    advisory = _advance_advisory(prev_phase, phase)

    # A backward/skip transition (advisory set AND the phase actually changes) is
    # confirmed before committing. Headless/unsupported clients proceed as before;
    # only an explicit decline aborts, leaving the project untouched.
    if (
        advisory
        and prev_phase != phase
        and not await elicit.confirm(
            ctx,
            f"{advisory} Proceed with the advance?",
            default_when_unsupported=True,
            title="Advance anyway?",
        )
    ):
        current = before[0] if before else {"slug": slug, "phase": prev_phase}
        return {**current, "advisory": advisory, "advanced": False}

    await _offload(
        client.change,
        "mutations.gq",
        "update_workflow_project_phase",
        {"slug": slug, "phase": phase, "github_pr": github_pr, "updated_at": now},
    )
    rows = await _offload(
        client.read, "read.gq", "get_workflow_project", {"slug": slug}
    )
    result = rows[0] if rows else {"slug": slug, "phase": phase}

    # Soft validation: flag an unusual transition without blocking it.
    if advisory:
        result["advisory"] = advisory
    return result


@_tool
async def workflow_project_complete(
    slug: str,
    outcome: str,
    github_pr: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """
    Mark a workflow project as completed and assemble its corpus trace.

    This creates a ``WorkflowTrace`` node that aggregates all linked sessions
    into an immutable record for later pattern mining. Idempotent: if a trace
    already exists for this project, it is returned without re-inserting.

    Parameters
    ----------
    slug:
        The ``wp-`` slug of the project to complete.
    outcome:
        Free-text narrative of what was delivered. Be specific — this is
        the primary content of the corpus record.
    github_pr:
        URL of the merged PR, if applicable.
    """
    now = now_iso()

    trace_slug = f"wt-{slug}"
    existing = await _offload(client.read, "read.gq", "get_trace", {"slug": trace_slug})
    if existing:
        return {"project_slug": slug, "trace_slug": trace_slug, "existed": True}

    # Completing seals an immutable corpus trace; a thin outcome makes a poor
    # permanent record. When it's terse, offer to expand it. Headless/unsupported
    # clients (or a decline) keep the provided outcome — nothing is blocked.
    if len(outcome.strip()) < _THIN_OUTCOME_CHARS:
        outcome = await elicit.text(
            ctx,
            f"Completing {slug} writes an immutable corpus trace. The current "
            f"outcome is brief ({outcome!r}). Provide a fuller narrative of what "
            "was delivered:",
            default=outcome,
            title="Outcome narrative",
        )

    await _offload(
        client.change,
        "mutations.gq",
        "update_workflow_project_complete",
        {
            "slug": slug,
            "status": "completed",
            "github_pr": github_pr,
            "completed_at": now,
            "updated_at": now,
        },
    )

    project_rows = await _offload(
        client.read, "read.gq", "get_workflow_project", {"slug": slug}
    )
    project = project_rows[0] if project_rows else {}

    sessions = await _offload(_project_sessions, slug)

    session_count = len(sessions)
    phases_seen = list(
        dict.fromkeys(s.get("phase") for s in sessions if s.get("phase"))
    )

    duration: int | None = None
    if sessions:
        started_vals = [s.get("started_at") for s in sessions if s.get("started_at")]
        ended_vals = [s.get("ended_at") for s in sessions if s.get("ended_at")]
        if started_vals and ended_vals:
            try:
                first = datetime.fromisoformat(min(started_vals))
                last = datetime.fromisoformat(max(ended_vals))
                duration = max(1, int((last - first).total_seconds() / 3600))
            except (ValueError, TypeError):
                pass

    await _offload(
        client.change_many,
        [
            (
                "mutations.gq",
                "insert_workflow_trace",
                {
                    "slug": trace_slug,
                    "project_slug": slug,
                    "repos": _project_repos(project) or None,
                    "title": project.get("title", slug),
                    "description": project.get("description", ""),
                    "session_count": session_count,
                    "phases": phases_seen,
                    "duration": duration,
                    "outcome": outcome,
                    "lessons_slug": None,
                    "patterns_slug": None,
                    "author": _current_author(),
                    "tags": project.get("tags"),
                    "created_at": now,
                },
            ),
            ("mutations.gq", "link_produced", {"from": slug, "to": trace_slug}),
        ],
    )

    return {"project_slug": slug, "trace_slug": trace_slug, "existed": False}


@_tool
def workflow_project_link_memory(project_slug: str, memory_slug: str) -> dict:
    """
    Link a memory to a workflow project (the ``Informed`` edge).

    Records that a project consulted or produced a memory — a ``pattern``,
    ``lesson``, ``project_fact``, or ``agent_context``. The linked memories
    surface when the project's corpus trace is mined for reusable patterns.

    Parameters
    ----------
    project_slug:
        The ``wp-`` slug of the project.
    memory_slug:
        The ``pat-`` / ``les-`` / ``pf-`` / ``ctx-`` slug returned by ``memory_store``.
    """
    client.change(
        "mutations.gq",
        "link_informed",
        {"from": project_slug, "to": memory_slug},
    )
    _invalidate_edge_index()  # Informed feeds corroboration
    return {"project_slug": project_slug, "memory_slug": memory_slug}


@_tool
def workflow_project_memories(
    project_slug: str, group_by_session: bool = False
) -> dict:
    """
    "What did we learn during project X" — the provenance walk.

    Assembles the memories connected to a project from two grains:
    - **session-grain** (``SessionProduced``): memories the project's sessions
      created, auto-recorded by ``memory_store`` when a session is active;
    - **project-grain** (``Informed``): memories explicitly linked via
      ``workflow_project_link_memory``.

    De-duplicated by slug. The flat ``memories`` list is assembled with two
    queries regardless of session count. Pass ``group_by_session=True`` to also
    get a ``by_session`` breakdown — that costs one extra query per session, so
    it is opt-in.

    Parameters
    ----------
    project_slug:
        The ``wp-`` slug whose memories to assemble.
    group_by_session:
        Also return a ``by_session`` breakdown of which session produced what.
        Costs one extra query per session, so leave it off unless you need the
        attribution.

    Returns ``{"project_slug": ..., "memories": [...], "by_session": {...}}``
    (``by_session`` is empty unless ``group_by_session`` is set).
    """
    merged: dict[str, dict] = {}
    for query in ("project_produced_memories", "informed_memories"):
        for row in client.read("read.gq", query, {"project_slug": project_slug}):
            merged[row["slug"]] = row

    by_session: dict[str, list[dict]] = {}
    if group_by_session:
        sessions = _project_sessions(project_slug)
        for session in sessions:
            produced = client.read(
                "read.gq",
                "session_produced_memories",
                {"session_slug": session["slug"]},
            )
            if produced:
                by_session[session["slug"]] = produced

    return {
        "project_slug": project_slug,
        "memories": list(merged.values()),
        "by_session": by_session,
    }


# ── Workflow Traces (corpus) ───────────────────────────────────────


@_tool
def workflow_trace_list(
    repo: str | None = None,
    tags: list[str] | None = None,
    author: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """
    List corpus WorkflowTrace records, optionally filtered by repo, tags, or author.

    Traces are otherwise only reachable by slug (``wt-<project-slug>``) via
    ``get_trace`` — this is the discovery path for browsing or mining across
    many completed projects (e.g. as onboarding case studies of how a project
    went end to end).

    Parameters
    ----------
    repo:
        Canonical repo URI to filter to (membership test against each trace's
        repo set). Auto-detected from ``.git/config`` if omitted. Pass an
        empty string to list traces across all repos.
    tags:
        Only return traces that carry ALL of these tags.
    author:
        Only return traces created by this author.
    limit:
        Max rows to return (applied after filtering).
    """
    if isinstance(tags, str):
        tags = [tags]
    detected_repo = repo_module.detect(override=repo)
    rows = client.read("read.gq", "list_all_traces", {})

    if detected_repo:
        rows = [r for r in rows if detected_repo in _project_repos(r)]
    if tags:
        rows = [r for r in rows if set(tags) <= set(r.get("tags") or [])]
    if author:
        rows = [r for r in rows if r.get("author") == author]

    return rows[:limit]


@_tool
def workflow_trace_get(slug: str) -> dict | None:
    """
    Retrieve a single corpus WorkflowTrace by slug.

    Slug handling is simple: a slug already starting with ``wt-`` is used as-is;
    **any other** slug is prefixed with ``wt-``. So the trace slug
    (``wt-<project-slug>``) and the project slug (``wp-<project-slug>``, which
    becomes ``wt-wp-<project-slug>``) both resolve to the same trace, and callers
    never have to hand-construct the ``wt-`` slug (the fragile step the
    ``witan-project-tracker`` skill used to instruct). Returns the full trace
    node (title/description/outcome, ``session_count``, ``phases``, ``duration``,
    and any mined ``lessons_slug``/``patterns_slug``) or ``None`` if no trace
    exists — a project only has a trace once ``workflow_project_complete`` has
    sealed it.

    Parameters
    ----------
    slug:
        The ``wt-`` trace slug, or the ``wp-`` project slug it was minted from
        (anything not already ``wt-``-prefixed gets the prefix added).
    """
    trace_slug = slug if slug.startswith("wt-") else f"wt-{slug}"
    rows = client.read("read.gq", "get_trace", {"slug": trace_slug})
    return rows[0] if rows else None


def _annotate_trace_step(
    trace_slug: str,
    lessons_slug: list[str] | None = None,
    patterns_slug: list[str] | None = None,
) -> tuple[_Step, dict] | None:
    """Build the union-merge step for annotating a trace, without committing it.

    Split out of ``_annotate_trace`` so ``workflow_trace_mine`` can land this
    step in the SAME commit as the ``link_informed`` edges it writes for newly
    mined memories, rather than one more trailing commit after them — the same
    "return the mutation, let the caller commit it" shape as the CodeBranch
    step builders. Returns ``None`` when the trace does not exist, leaving the
    caller to decide what that means for its own return value.
    """
    if isinstance(lessons_slug, str):
        lessons_slug = [lessons_slug]
    if isinstance(patterns_slug, str):
        patterns_slug = [patterns_slug]
    rows = client.read("read.gq", "get_trace", {"slug": trace_slug})
    if not rows:
        return None
    trace = rows[0]

    merged_lessons = list(
        dict.fromkeys([*(trace.get("lessons_slug") or []), *(lessons_slug or [])])
    )
    merged_patterns = list(
        dict.fromkeys([*(trace.get("patterns_slug") or []), *(patterns_slug or [])])
    )
    step: _Step = (
        "mutations.gq",
        "update_workflow_trace_annotations",
        {
            "slug": trace_slug,
            "lessons_slug": merged_lessons or None,
            "patterns_slug": merged_patterns or None,
        },
    )
    result = {
        "slug": trace_slug,
        "lessons_slug": merged_lessons or None,
        "patterns_slug": merged_patterns or None,
    }
    return step, result


def _annotate_trace(
    trace_slug: str,
    lessons_slug: list[str] | None = None,
    patterns_slug: list[str] | None = None,
) -> dict:
    """Union new lesson/pattern slugs into a trace's annotation fields."""
    built = _annotate_trace_step(
        trace_slug, lessons_slug=lessons_slug, patterns_slug=patterns_slug
    )
    if built is None:
        return {"slug": trace_slug, "error": "no such trace"}
    step, result = built
    client.change(*step)
    return result


@_tool
def workflow_trace_annotate(
    trace_slug: str,
    lessons_slug: list[str] | None = None,
    patterns_slug: list[str] | None = None,
) -> dict:
    """
    Append lesson/pattern memory slugs to an existing WorkflowTrace.

    Lets an agent (or ``workflow_trace_mine``) record which Memory nodes a completed
    project's trace produced without re-running ``workflow_project_complete``
    (traces are otherwise immutable after creation). Unions with whatever is
    already recorded, so it's safe to call repeatedly as more lessons/patterns
    are mined over time.

    Parameters
    ----------
    trace_slug:
        The ``wt-`` slug of the trace to annotate.
    lessons_slug:
        ``les-`` memory slugs to add to the trace's ``lessons_slug`` field.
    patterns_slug:
        ``pat-`` memory slugs to add to the trace's ``patterns_slug`` field.
    """
    return _annotate_trace(
        trace_slug, lessons_slug=lessons_slug, patterns_slug=patterns_slug
    )


@_tool
def workflow_trace_mine(
    trace_slug: str,
    patterns: list[dict] | None = None,
    lessons: list[dict] | None = None,
    session_slug: str | None = None,
) -> dict:
    """
    Turn a completed WorkflowTrace into reusable Pattern/Lesson Memory nodes.

    Call with no ``patterns``/``lessons`` first — returns the trace itself
    (title, description, outcome) plus every session summary from its project,
    the raw material to mine for reusable knowledge. Review that, then call
    again with the patterns/lessons you propose to persist them: each becomes
    a ``Memory`` node, gets an ``Informed`` edge back to the trace's project,
    and its slug is appended to the trace's ``lessons_slug``/``patterns_slug``
    fields.

    These mined memories are read by other agents for self-improvement, but
    are equally a corpus of worked examples for people onboarding onto this
    system — write ``title``/``content`` so a newcomer unfamiliar with the
    project can follow the reasoning, not just a terse note for a future agent.

    Parameters
    ----------
    trace_slug:
        The ``wt-`` slug of the trace to mine.
    patterns:
        Proposed pattern memories to create on this call. Each dict needs
        ``title`` and ``content``; may also include ``repo``, ``language``,
        and ``tags``.
    lessons:
        Proposed lesson memories to create on this call. Each dict needs
        ``title`` and ``content``; may also include ``repo``, ``severity``,
        and ``tags``.
    session_slug:
        The ``ws-`` handle from ``workflow_session_start``, recorded as the
        provenance of every memory mined on this call — see ``memory_store``.

    Returns
    -------
    Without proposals: ``{"trace": ..., "sessions": [...]}``.
    With proposals: ``{"created_patterns": [...], "created_lessons": [...]}``
    — the slugs of the memories just created.
    """
    rows = client.read("read.gq", "get_trace", {"slug": trace_slug})
    if not rows:
        return {"slug": trace_slug, "error": "no such trace"}
    trace = rows[0]

    if isinstance(patterns, dict):
        patterns = [patterns]
    if isinstance(lessons, dict):
        lessons = [lessons]

    if patterns is None and lessons is None:
        sessions = _project_sessions(trace["project_slug"])
        return {"trace": trace, "sessions": sessions}

    for label, specs in (("pattern", patterns), ("lesson", lessons)):
        for spec in specs or []:
            if (
                not isinstance(spec, dict)
                or "title" not in spec
                or "content" not in spec
            ):
                raise ValueError(
                    f"Each proposed {label} must be a dict containing 'title' and 'content', got {spec!r}"
                )

    created_patterns = [
        _store_memory(
            "pattern",
            spec["title"],
            spec["content"],
            repo=spec.get("repo"),
            language=spec.get("language"),
            tags=spec.get("tags"),
            session_slug=session_slug,
        )["slug"]
        for spec in patterns or []
    ]
    created_lessons = [
        _store_memory(
            "lesson",
            spec["title"],
            spec["content"],
            repo=spec.get("repo"),
            severity=spec.get("severity"),
            tags=spec.get("tags"),
            session_slug=session_slug,
        )["slug"]
        for spec in lessons or []
    ]

    # One commit for every trailing write, not N+1: an `Informed` edge per
    # mined memory plus the trace's own annotation update used to be N+1
    # separate commits after the (unavoidably separate, one per row)
    # `_store_memory` calls above.
    steps: list[_Step] = [
        ("mutations.gq", "link_informed", {"from": trace["project_slug"], "to": slug})
        for slug in (*created_patterns, *created_lessons)
    ]
    if created_patterns or created_lessons:
        _invalidate_edge_index()  # Informed feeds corroboration

    annotate = _annotate_trace_step(
        trace_slug,
        lessons_slug=created_lessons or None,
        patterns_slug=created_patterns or None,
    )
    if annotate is not None:
        steps.append(annotate[0])
    client.change_many(steps)

    return {"created_patterns": created_patterns, "created_lessons": created_lessons}


@_tool
def workflow_project_block(slug: str, blocks_slug: str) -> dict:
    """
    Declare that one project must complete before another can begin.

    Project-level sequencing — coarse ordering between whole projects. For
    fine-grained ordering between individual tasks use ``task_link(kind="blocks")``;
    the two are deliberately separate (Project vs Task nodes).

    Adds a ``ProjectBlocks`` graph edge (``slug`` → ``blocks_slug``) and
    appends ``slug`` to ``blocks_slug.blocked_by`` so the ready-work check
    in ``workflow_project_list`` can filter without traversing edges.

    Parameters
    ----------
    slug:
        The ``wp-`` slug of the *blocking* project (must finish first).
    blocks_slug:
        The ``wp-`` slug of the project being *blocked*.
    """
    if slug == blocks_slug:
        return {
            "blocker": slug,
            "blocked": blocks_slug,
            "linked": False,
            "reason": "a project cannot block itself",
        }
    now = now_iso()
    # The edge always lands; the denormalized `blocked_by` sync only when
    # there's something new to add. Read BEFORE writing so both can land in
    # ONE commit via `change_many` when the sync applies — the edge write
    # doesn't touch `blocks_slug`'s own row, so reading it first changes
    # nothing about what's read.
    steps: list[_Step] = [
        ("mutations.gq", "link_project_blocks", {"from": slug, "to": blocks_slug})
    ]
    blocked = client.read("read.gq", "get_workflow_project", {"slug": blocks_slug})
    if blocked:
        existing = blocked[0].get("blocked_by") or []
        if slug not in existing:
            steps.append(
                (
                    "mutations.gq",
                    "update_workflow_project_blocked_by",
                    {
                        "slug": blocks_slug,
                        "blocked_by": [*existing, slug],
                        "updated_at": now,
                    },
                )
            )
    client.change_many(steps)
    return {"blocker": slug, "blocked": blocks_slug, "linked": True}


@_tool
def workflow_project_unblock(slug: str, blocks_slug: str) -> dict:
    """
    Remove a project dependency declared with ``workflow_project_block``.

    Removes ``slug`` from ``blocks_slug.blocked_by`` AND deletes the
    ``ProjectBlocks`` edge, so the graph and the denormalized field (which
    drives the ready-work check) agree.

    Earlier versions left the edge in place, on the belief that omnigraph
    edges were append-only. They are not — see ``_unlink_edge``.

    Parameters
    ----------
    slug:
        The ``wp-`` slug of the *blocking* project to remove.
    blocks_slug:
        The ``wp-`` slug of the project to unblock.
    """
    now = now_iso()
    _unlink_edge("project_blocks", slug, blocks_slug)
    blocked = client.read("read.gq", "get_workflow_project", {"slug": blocks_slug})
    if blocked:
        existing = blocked[0].get("blocked_by") or []
        updated = [s for s in existing if s != slug]
        client.change(
            "mutations.gq",
            "update_workflow_project_blocked_by",
            {
                "slug": blocks_slug,
                "blocked_by": updated or None,
                "updated_at": now,
            },
        )
    return {
        "blocker": slug,
        "blocked": blocks_slug,
        "removed": slug in (blocked[0].get("blocked_by") or [] if blocked else []),
    }


@_tool
def workflow_project_get_blockers(slug: str) -> list[dict]:
    """
    Return all projects that are blocking the given project.

    Resolves each slug in the project's ``blocked_by`` list and returns the
    full node for each blocker, including its current status.

    Parameters
    ----------
    slug:
        The ``wp-`` slug of the project to check.
    """
    rows = client.read("read.gq", "get_workflow_project", {"slug": slug})
    if not rows:
        return []
    blocked_by = rows[0].get("blocked_by") or []
    result = []
    for blocker_slug in blocked_by:
        blocker_rows = client.read(
            "read.gq", "get_workflow_project_by_slug", {"slug": blocker_slug}
        )
        if blocker_rows:
            result.append(blocker_rows[0])
    return result


def _project_blocker_status(blocker_slug: str, status_cache: dict[str, str]) -> str:
    """Return the status of a blocking project, using a cache to avoid repeated reads."""
    if blocker_slug in status_cache:
        return status_cache[blocker_slug]
    fetched = client.read(
        "read.gq", "get_workflow_project_by_slug", {"slug": blocker_slug}
    )
    status = fetched[0].get("status", "completed") if fetched else "completed"
    status_cache[blocker_slug] = status
    return status


def _project_is_ready(p: dict, status_cache: dict[str, str]) -> bool:
    """True when a project has no incomplete upstream blockers."""
    blocked_by = p.get("blocked_by") or []
    return all(
        _project_blocker_status(b, status_cache) == "completed" for b in blocked_by
    )


def _open_session_for_key(project_slug: str, session_id: str) -> dict | None:
    """The still-open session already recorded for this (project, session_id).

    "Still open" — ``ended_at`` unset — is deliberately narrower than the pair
    alone. The pair is *not* unique in practice: one ``$CLAUDE_SESSION_ID``
    routinely spans several working stints, each closed with its own summary
    (the corpus has clusters of eight such sessions, each a distinct piece of
    work). Keying idempotency on the pair alone would fold those into one node
    and destroy seven summaries. A retry, reconnect or replica failover, by
    contrast, always re-fires *before* the first call was ended — so an open
    session with the same pair is the duplicate this must not mint, and a closed
    one is a finished stint this must not touch.

    Sessions already flagged by ``witan migrate dedupe-sessions`` are skipped;
    the newest open match wins if somehow several survive.
    """
    rows = client.read(
        "read.gq",
        "sessions_for_key",
        {"project_slug": project_slug, "session_id": session_id},
    )
    open_rows = [
        r for r in rows if not r.get("ended_at") and not r.get("superseded_by")
    ]
    return open_rows[-1] if open_rows else None


def _dedupe_open_sessions(
    project_slug: str, session_id: str, slug: str, started_at: str
) -> tuple[str, str]:
    """Collapse open sessions a concurrent start raced into existence.

    The check-then-insert above is not atomic: two starts for one
    (project, session_id) — a client retrying while the first request is still
    in flight, or two replicas handling the same retry — can both find no open
    session and both insert. The engine can't arbitrate that the way it does for
    ``task_claim``: optimistic concurrency detects competing writes to *one*
    row, and these are two rows under two freshly-minted slugs.

    So resolve it after the fact. Writes serialize through the store, and a
    reader sees every write that preceded it, so the racer who inserted second
    necessarily sees both rows. It keeps the earliest-started as canonical and
    supersedes the rest — a rule both racers compute identically, so they
    converge on the same handle no matter which of them observes the collision.
    Returns the (possibly reassigned) slug and its start time.

    Costs one extra read per *new* session; the re-entrant path never reaches
    here. Best-effort: a failure to read or mark leaves the duplicate for
    ``witan migrate dedupe-sessions``, and must not fail the session start.
    """
    try:
        rows = client.read(
            "read.gq",
            "sessions_for_key",
            {"project_slug": project_slug, "session_id": session_id},
        )
    except RuntimeError:
        return slug, started_at

    open_rows = [
        r for r in rows if not r.get("ended_at") and not r.get("superseded_by")
    ]
    if len(open_rows) < 2:
        return slug, started_at

    canonical = min(open_rows, key=lambda r: r.get("started_at") or "")
    for row in open_rows:
        if row["slug"] != canonical["slug"]:
            try:
                client.change(
                    "mutations.gq",
                    "update_workflow_session_superseded",
                    {"slug": row["slug"], "superseded_by": canonical["slug"]},
                )
            except RuntimeError:
                pass
    return canonical["slug"], canonical.get("started_at") or started_at


@_tool
def workflow_session_start(
    project_slug: str,
    session_id: str,
    phase: WorkflowPhase,
    repo: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """
    Link the current agent session to a workflow project.

    Call this at the start of any session that is contributing to a tracked
    project. The context injected by the context-injection hook (Claude Code) or
    extension (Pi) provides the ``project_slug``; ``session_id`` should be the
    session id — ``$CLAUDE_SESSION_ID`` on Claude Code, or any stable unique
    string for the session otherwise.

    Returns an explicit session handle (``session_slug``, ``project_slug``,
    ``phase``, ``session_id``, ``started_at``). Hold on to it and pass
    ``session_slug`` back to ``workflow_session_end`` — the handle is the only
    thing that ties the two calls together, since the protocol carries no session
    state of its own and consecutive calls may land on different replicas.

    **Re-entrant.** Calling again for a (``project_slug``, ``session_id``) whose
    session is still open returns that same handle with ``existed: true``
    instead of minting a second node — so a hook retry, a transport reconnect,
    or the replica failover the paragraph above warns about can't silently
    duplicate a session. Any newly-supplied ``repo`` and ``tags`` are merged
    into the existing session; ``phase`` is left at what the first call set (use
    ``workflow_project_advance`` to move a project's phase). Once a session has
    been ended, the same ``session_id`` starts a fresh session — one
    ``$CLAUDE_SESSION_ID`` legitimately spans several working stints.

    Two *simultaneous* starts (a client retrying while the first request is
    still in flight) can still both insert, since the check and the insert are
    not one atomic operation. That is resolved immediately after the fact rather
    than left for the migration to find — see ``_dedupe_open_sessions``. Both
    racers return the same handle.

    Because the repo accretion below runs on the re-entrant path too, calling
    this once per repo remains a valid way to widen a project's repo set — but
    ``workflow_project_update(add_repos=[...])`` does it directly, without
    needing a session at all.

    When a repo is detected and the checkout is on a git branch, also
    upserts a ``CodeBranch`` (repo, branch) and links it ``ForProject`` to
    this project — schema.pg § Code Branches. Best-effort: silently skipped
    with no repo/branch context, never fails the session start.

    Parameters
    ----------
    project_slug:
        The ``wp-`` slug of the project this session belongs to.
    session_id:
        Unique identifier for this agent session.
    phase:
        The phase this session is working in.
    repo:
        Repo scoping — see instructions.
    tags:
        Optional tags.
    """
    now = now_iso()
    detected_repo = repo_module.detect(override=repo)
    existing = _open_session_for_key(project_slug, session_id)

    # ONE commit for everything this call writes. Each of the four writes below
    # used to be its own `mutate` — insert+edge, the project's repo set, the
    # CodeBranch upsert, the ForProject edge — and against the deployed store
    # each costs ~3.5-4s, so a session start could spend 14-16s of ToolHive's
    # 30s deadline before another user contended for anything. They are
    # independent rows with no read-back between them, which is exactly the
    # shape `change_many` exists for.
    steps: list[_Step] = []

    if existing:
        slug = existing["slug"]
        # Merge, never clear: a repeat call that omits repo/tags must not wipe
        # what the first one recorded.
        merged_repo = detected_repo or existing.get("repo")
        merged_tags = list(
            dict.fromkeys([*(existing.get("tags") or []), *(tags or [])])
        )
        if merged_repo != existing.get("repo") or merged_tags != (
            existing.get("tags") or []
        ):
            steps.append(
                (
                    "mutations.gq",
                    "update_workflow_session_meta",
                    {"slug": slug, "repo": merged_repo, "tags": merged_tags or None},
                )
            )
        phase = existing.get("phase") or phase
        started_at = existing.get("started_at") or now
    else:
        slug = _make_slug("workflow_session", project_slug)
        started_at = now
        steps += [
            (
                "mutations.gq",
                "insert_workflow_session",
                {
                    "slug": slug,
                    "project_slug": project_slug,
                    "session_id": session_id,
                    "repo": detected_repo,
                    "phase": phase,
                    "summary": "",
                    "author": _current_author(),
                    "tags": tags,
                    "started_at": now,
                },
            ),
            (
                "mutations.gq",
                "link_belongs_to",
                {"from": slug, "to": project_slug},
            ),
        ]

    # Accrete this session's repo into the project's repo set, so a project's
    # association grows as it's worked across repos without explicit declaration.
    if detected_repo:
        project_rows = client.read(
            "read.gq", "get_workflow_project", {"slug": project_slug}
        )
        if project_rows:
            current = _project_repos(project_rows[0])
            if detected_repo not in current:
                steps.append(
                    (
                        "mutations.gq",
                        "update_workflow_project_repos",
                        {
                            "slug": project_slug,
                            "repos": _merge_repos(current, detected_repo),
                            "updated_at": now,
                        },
                    )
                )

    steps += _code_branch_steps(detected_repo, project_slug=project_slug)

    # Empty only when there is nothing to say: a re-entrant call with no meta
    # change, no new repo for the project, and no checkout to track. A
    # re-entrant call ON a git branch still writes — the CodeBranch liveness
    # touch — so this guard is not a "nothing changed" fast path.
    if steps:
        client.change_many(steps)

    # AFTER the commit, and only for a new session: the dedupe reads
    # `sessions_for_key` and has to see this insert plus any racer's, so it
    # cannot join the batch above — a read cannot observe a write that has not
    # committed. This is why the floor here is one commit and not zero.
    if not existing:
        slug, started_at = _dedupe_open_sessions(project_slug, session_id, slug, now)

    handle = {
        "session_slug": slug,
        "project_slug": project_slug,
        "phase": phase,
        "session_id": session_id,
        "started_at": started_at,
        "existed": existing is not None,
    }
    # Convenience only, and only when the server is the user's own stdio process:
    # then it shares a filesystem with the Stop hook, so it can park the handle
    # itself and a bare `workflow_session_start` tool call still auto-closes. A
    # deployed replica shares nothing with the hook — the client persists the
    # returned handle instead (``witan session start``). See ``session_state``.
    if _is_local_stdio():
        session_state.write_handle(session_id, handle)

    return handle


@_tool
def workflow_session_end(
    session_slug: str,
    summary: str,
    tools_used: list[str] | None = None,
    files_changed: list[str] | None = None,
) -> dict:
    """
    Close the current session with a summary of work accomplished.

    Call this before ending a session to produce a high-quality corpus record.
    The ``Stop`` hook will auto-close sessions that did not call this, but
    with a placeholder summary.

    For best corpus quality, write a summary that includes:
    - What was done this session
    - What remains for the next session
    - Any blockers or decisions made

    Parameters
    ----------
    session_slug:
        The ``ws-`` slug returned by ``workflow_session_start``.
    summary:
        Description of what was accomplished and what remains.
    tools_used:
        List of tool names used. e.g. ``["Edit", "Bash", "Read"]``.
    files_changed:
        List of file paths modified in this session.
    """
    now = now_iso()
    client.change(
        "mutations.gq",
        "update_workflow_session_end",
        {
            "slug": session_slug,
            "summary": summary,
            "tools_used": tools_used,
            "files_changed": files_changed,
            "ended_at": now,
        },
    )

    # Drop the local handle so the Stop hook doesn't re-close this session.
    # Local-stdio only — a deployed replica would be scanning its own container's
    # temp dir, where the client's handle was never written; the client clears
    # its own copy (``witan session end`` / ``witan session-checkpoint``).
    if _is_local_stdio():
        session_state.clear_handle_for_slug(session_slug)

    return {"session_slug": session_slug, "ended_at": now}


@_tool
def workflow_session_list(
    project_slug: str | None = None,
    open_only: bool = False,
    include_superseded: bool = False,
) -> list[dict]:
    """
    List workflow sessions, newest last.

    Mainly for finding sessions that leaked open — one whose agent died, or
    whose Stop hook could not reach the graph. An open session is not cosmetic:
    ``workflow_project_complete`` folds every linked session into the corpus
    trace, so one with no ``ended_at`` inflates ``session_count``, contributes
    its phase while having recorded nothing, carries no handoff summary, and
    cannot extend ``duration`` (computed from ``max(ended_at)``). It is also
    what drives the context hook's "N sessions in <phase>" staleness nag.

    Use ``witan session sweep`` to close them in bulk.

    Parameters
    ----------
    project_slug:
        Restrict to one project's sessions. Omit for every project.
    open_only:
        Only sessions with no ``ended_at``. Superseded sessions (deduped by
        ``witan migrate dedupe-sessions``) are always excluded — they are
        already skipped by every aggregate read and are not leaks.
    include_superseded:
        Keep superseded rows instead of dropping them. For ``witan session
        list``, the one caller that wants to see what
        ``migrate dedupe-sessions`` did rather than the leaked-session view.
    """
    if project_slug:
        rows = client.read(
            "read.gq", "list_sessions_by_project", {"project_slug": project_slug}
        )
        # That query filters ON project_slug so it doesn't return the column.
        # Put it back, so a caller sees one row shape either way.
        rows = [{**r, "project_slug": project_slug} for r in rows]
    else:
        rows = client.read("read.gq", "list_all_sessions", {})
    if not include_superseded:
        rows = [r for r in rows if not r.get("superseded_by")]
    if open_only:
        rows = [r for r in rows if not r.get("ended_at")]
    return rows


# ── Task Tracking Tools ───────────────────────────────────────────
#
# A dependency-aware tracker living in the same graph as memory and workflow.
# Tasks are hierarchical (epic → sub-issue via `parent`) and can block one
# another; `task_ready` surfaces open tasks whose blockers are all closed.

TaskType = Literal["bug", "feature", "task", "chore", "epic"]
TaskStatus = Literal["open", "in_progress", "blocked", "closed"]
TaskPriority = Literal["p0", "p1", "p2", "p3"]
TaskLinkKind = Literal["blocks", "parent", "discovered_from", "addresses"]

_PRIORITY_ORDER = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}


def _unblock_dependents(repo: str | None) -> None:
    """Flip ``blocked`` tasks back to ``open`` once all their blockers are closed.

    Called after a task closes so ``task_list`` status stays truthful. Scoped to the
    closed task's repo (blockers and dependents share a repo in practice).
    """
    rows = (
        client.read("read.gq", "list_tasks_by_repo", {"repo": repo})
        if repo
        else client.read("read.gq", "list_unscoped_tasks", {})
    )
    status_by_slug = {r["slug"]: r.get("status") for r in rows}

    def is_closed(blocker_slug: str) -> bool:
        if blocker_slug in status_by_slug:
            return status_by_slug[blocker_slug] == "closed"
        fetched = client.read("read.gq", "get_task", {"slug": blocker_slug})
        return fetched[0].get("status") == "closed" if fetched else True

    for r in rows:
        blockers = r.get("blocked_by") or []
        if r.get("status") == "blocked" and all(is_closed(b) for b in blockers):
            _update_task(r["slug"], {"status": "open"})


def _update_task(
    slug: str,
    changes: dict,
    *,
    surface_conflict: bool = False,
    conditional: bool = False,
    extra_steps: list[_Step] | None = None,
) -> tuple[dict | None, str | None]:
    """Read a task, merge ``changes`` over its mutable fields, write it back.

    Mirrors the read-merge-write pattern documented for ``update_memory`` so we
    avoid per-field update queries. Returns ``(updated_node, new_commit)`` —
    the node is ``None`` when the task did not exist (nothing was written).
    ``new_commit`` is the ``graph_commit_id`` THIS write produced. Non-``None``
    for any single-step write over HTTP whenever the server supplies one —
    regardless of ``conditional`` — since ``change()`` reads it straight out
    of the mutate response; ``None`` only for the CLI transport (never
    supplies one), a server predating omnigraph #470, or ``extra_steps``
    (routed through ``change_many``, which does not return a commit). See
    :meth:`change`. ``task_claim`` uses this as the floor its post-write
    verification's own re-read must catch up to before being trusted — see
    the call site. Every caller before ``task_claim`` ignored the row too, so
    this widened almost nobody's call site — see the tuple-unpack at each of
    the two callers that actually use the row.

    ``surface_conflict`` propagates to the write so a compare-and-swap caller
    (``task_claim``) sees an :class:`~witan.graph.OmnigraphConflict` on a lost
    optimistic-concurrency race instead of the write being silently retried.

    ``conditional`` makes the write a real compare-and-swap: it is applied only
    while the branch head this function READ is still current, and otherwise
    refused terminally, having changed nothing (omnigraph #470). Combined with
    ``surface_conflict`` the refusal arrives as ``OmnigraphConflict``, which is
    what turns "I lost" from an inference into a fact.

    ★ IT DEGRADES TO THE OLD BEHAVIOUR RATHER THAN FAILING, and that is a
    deliberate choice worth knowing about. A server without #470 — or the CLI
    path, which prints rows and discards the envelope — supplies no
    ``graph_commit_id``, so there is no precondition to state and the write goes
    out unconditional. That is exactly today's best-effort claim, and
    ``task_claim``'s post-write verification still catches a clobber, so the
    fallback is safe rather than silently broken. It is NOT equivalent, though:
    losing becomes an inference again. Anything depending on the stronger
    guarantee must check that it is actually getting it.

    ``extra_steps`` ride in the SAME commit as the update — for a caller whose
    change is one logical edit spanning a row and an edge. ``task_update`` with
    a ``parent`` is that caller: it used to write the row, then the ParentOf
    edge, then the row again to record ``parent_slug``, which is three Lance
    commits (~3.5-4s each deployed) for one edit. They also cannot be seen
    half-applied now, which is the more important half: the edge and the
    ``parent_slug`` field are two encodings of one fact.

    A missing task still writes NOTHING, extras included — the early return
    below happens before any step is issued.
    """
    if conditional and extra_steps:
        # Refused rather than silently downgraded. `change_many` composes the
        # batch into one mutate, and pairing a whole-branch precondition with a
        # multi-step commit needs its own thought about what a partial refusal
        # means — nobody needs it yet, so the combination is an error instead of
        # a quietly unconditional write.
        msg = "conditional=True does not support extra_steps"
        raise ValueError(msg)
    rows, read_commit = client.read_with_commit("read.gq", "get_task", {"slug": slug})
    if not rows:
        return None, None
    current = rows[0]
    merged = {
        "slug": slug,
        "title": changes.get("title", current.get("title")),
        "description": changes.get("description", current.get("description")),
        "type": changes.get("type", current.get("type")),
        "status": changes.get("status", current.get("status")),
        "priority": changes.get("priority", current.get("priority")),
        "repo": changes.get("repo", current.get("repo")),
        "project_slug": changes.get("project_slug", current.get("project_slug")),
        "parent_slug": changes.get("parent_slug", current.get("parent_slug")),
        "blocked_by": changes.get("blocked_by", current.get("blocked_by")),
        "assignee": changes.get("assignee", current.get("assignee")),
        "external_uri": changes.get("external_uri", current.get("external_uri")),
        "resolution": changes.get("resolution", current.get("resolution")),
        "symbol_refs": changes.get("symbol_refs", current.get("symbol_refs")),
        "tags": changes.get("tags", current.get("tags")),
        "closed_at": changes.get("closed_at", current.get("closed_at")),
        "claimed_at": changes.get("claimed_at", current.get("claimed_at")),
        "updated_at": now_iso(),
    }
    update: _Step = ("mutations.gq", "update_task", merged)
    new_commit: str | None = None
    if extra_steps:
        client.change_many([update, *extra_steps], surface_conflict=surface_conflict)
    else:
        # Single-statement `change` on the bare path: the compare-and-swap
        # caller goes through here, and keeping its write exactly as it was
        # keeps `surface_conflict`'s behaviour untouched.
        #
        # ★ THE PRECONDITION IS THE COMMIT FROM *THIS* FUNCTION'S OWN READ, a
        # few lines up — not one the caller supplies. That is the invariant
        # that makes the compare-and-swap mean anything: `merged` is built from
        # THAT snapshot, so demanding the head has not moved since is exactly
        # "nothing changed under the values I am about to write back". A token
        # from any earlier read would fence the wrong interval.
        if conditional:
            # ★ THE OTHER HALF OF THE CLAIM TRACE. Pairing this with
            # `witan.task_claim.verify` is what makes the double-claim
            # investigation possible at all: for each racer you get the commit
            # it FENCED against and the commit its verification READ was served
            # at. A racer that wrote against a stale head, or verified at one,
            # is then visible rather than hypothesised.
            #
            # `read_commit is None` is itself a finding worth seeing — it means
            # the tier supplied no graph_commit_id and the write went out
            # UNCONDITIONAL, which is the documented degraded path and would
            # explain a lost mutual exclusion without any staleness at all.
            logger.info(
                "witan.task_update.conditional",
                task_slug=slug,
                assignee=changes.get("assignee"),
                if_graph_commit_id=read_commit,
                unconditional_fallback=read_commit is None,
            )
        new_commit = client.change(
            *update,
            surface_conflict=surface_conflict,
            if_commit=read_commit if conditional else None,
        )
    return client.read("read.gq", "get_task", {"slug": slug})[0], new_commit


@_tool
async def task_create(
    title: str,
    description: str,
    type: TaskType = "task",
    priority: TaskPriority = "p2",
    repo: str | None = None,
    project_slug: str | None = None,
    parent: str | None = None,
    blocked_by: list[str] | None = None,
    discovered_from: list[str] | None = None,
    external_uri: str | None = None,
    symbol_refs: list[str] | None = None,
    tags: list[str] | None = None,
    ctx: Context | None = None,
) -> dict:
    """
    Create a task in the work-coordination graph.

    Tasks are dependency-aware and hierarchical. Use ``parent`` to attach a
    sub-issue to an ``epic`` (or any parent task); use ``blocked_by`` to record
    dependencies so ``task_ready`` can withhold the task until its blockers
    close.

    Parameters
    ----------
    title, description:
        Short label and full text of the work.
    type:
        ``bug`` | ``feature`` | ``task`` | ``chore`` | ``epic``.
    priority:
        ``p0`` (highest) … ``p3``. Drives ``task_ready`` ordering.
    repo:
        Repo scoping — see instructions.
    project_slug:
        ``wp-`` slug of the WorkflowProject this task rolls up to.
    parent:
        ``tk-`` slug of the parent task/epic. Sets the hierarchy edge.
    blocked_by:
        ``tk-`` slugs that must close before this task is ready.
    discovered_from:
        ``tk-`` slugs of tasks during which this work was discovered.
    external_uri:
        A reference URI — e.g. a GitHub issue or PR.
    symbol_refs:
        Code-graph symbol ids (``repo#path::Name``) this task concerns.
    tags:
        Optional free-form tags.
    """
    now = now_iso()
    slug = _make_slug("task", title)
    # Offer to scope the task when nothing is detected; falls back to an
    # unscoped task (today's behavior) under automation / an unsupported client.
    repo = await elicit.repo_or_detect(ctx, repo)
    detected_repo = repo_module.detect(override=repo)
    # Only "blocked" if a blocker is not already closed — otherwise it's ready
    # now and would never be auto-unblocked.
    status: TaskStatus = "open"
    for blocker_slug in blocked_by or []:
        fetched = await _offload(
            client.read, "read.gq", "get_task", {"slug": blocker_slug}
        )
        if fetched and fetched[0].get("status") != "closed":
            status = "blocked"
            break

    # One commit for the task and every edge it arrives with — a task created
    # with a project, a parent and two blockers was five Lance versions. The
    # node is first because each edge below resolves an endpoint against it.
    steps: list[tuple[str, str, dict]] = [
        (
            "mutations.gq",
            "insert_task",
            {
                "slug": slug,
                "title": title,
                "description": description,
                "repo": detected_repo,
                "type": type,
                "status": status,
                "priority": priority,
                "project_slug": project_slug,
                "parent_slug": parent,
                "blocked_by": blocked_by,
                "assignee": None,
                "external_uri": external_uri,
                "author": _current_author(),
                "symbol_refs": symbol_refs,
                "tags": tags,
                "created_at": now,
                "updated_at": now,
                "claimed_at": None,
            },
        )
    ]
    if project_slug:
        steps.append(
            (
                "mutations.gq",
                "link_task_belongs_to",
                {"from": slug, "to": project_slug},
            )
        )
    if parent:
        steps.append(("mutations.gq", "link_parent_of", {"from": parent, "to": slug}))
    for blocker in blocked_by or []:
        steps.append(("mutations.gq", "link_blocks", {"from": blocker, "to": slug}))
    for source in discovered_from or []:
        steps.append(
            ("mutations.gq", "link_discovered_from", {"from": slug, "to": source})
        )
    await _offload(client.change_many, steps)

    return {"slug": slug, "status": status, "repo": detected_repo}


@_tool
def task_get(slug: str) -> dict | None:
    """Retrieve a single task by slug. Returns the full node or ``null``.

    Parameters
    ----------
    slug:
        The ``tk-`` slug to retrieve.
    """
    rows = client.read("read.gq", "get_task", {"slug": slug})
    return rows[0] if rows else None


@_tool
def task_list(
    repo: str | None = None,
    status: TaskStatus | None = None,
    project_slug: str | None = None,
    parent: str | None = None,
    assignee: str | None = None,
) -> list[dict]:
    """
    List tasks, filtered by repo, status, project, parent, and/or assignee.

    ``project_slug`` and ``parent`` take precedence as the primary scope; other
    filters are applied on top in Python. With no filters, lists recent tasks
    across all repos.

    Parameters
    ----------
    repo:
        Repo scoping — see instructions.
    status:
        ``open`` | ``in_progress`` | ``blocked`` | ``closed``.
    project_slug:
        List the tasks of a WorkflowProject.
    parent:
        List the direct children of a parent task/epic.
    assignee:
        Filter to a single owner.
    """
    if project_slug:
        rows = client.read(
            "read.gq", "list_tasks_by_project", {"project_slug": project_slug}
        )
    elif parent:
        rows = client.read("read.gq", "list_tasks_by_parent", {"parent_slug": parent})
    else:
        detected = repo_module.detect(override=repo)
        if detected:
            # Include repo-scoped tasks and unscoped (repo=null) tasks. Unscoped
            # tasks were created without git context (e.g. via MCP from a global
            # server) and should be visible regardless of which repo you're in.
            if status:
                repo_rows = client.read(
                    "read.gq",
                    "list_tasks_by_repo_status",
                    {"repo": detected, "status": status},
                )
            else:
                repo_rows = client.read(
                    "read.gq", "list_tasks_by_repo", {"repo": detected}
                )
            all_rows = client.read("read.gq", "list_unscoped_tasks", {})
            seen = {r["slug"] for r in repo_rows}
            unscoped = [
                r for r in all_rows if not r.get("repo") and r["slug"] not in seen
            ]
            rows = repo_rows + unscoped
        elif status:
            rows = client.read("read.gq", "list_tasks_by_status", {"status": status})
        else:
            rows = client.read("read.gq", "list_all_tasks", {})

    if status:
        rows = [r for r in rows if r.get("status") == status]
    if assignee:
        rows = [r for r in rows if _holder_matches(r.get("assignee"), assignee)]
    return rows


@_tool
def task_update(
    slug: str,
    title: str | None = None,
    description: str | None = None,
    type: TaskType | None = None,
    status: TaskStatus | None = None,
    priority: TaskPriority | None = None,
    repo: str | None = None,
    assignee: str | None = None,
    project_slug: str | None = None,
    parent: str | None = None,
    external_uri: str | None = None,
    symbol_refs: list[str] | None = None,
    tags: list[str] | None = None,
) -> dict | None:
    """
    Update a task's mutable fields. Only non-null arguments are applied.

    Use this to re-prioritise, re-parent (``parent``), reassign (``assignee``),
    correct the repo association (``repo``), or attach an ``external_uri``. To
    *claim* a task for work prefer ``task_claim`` (it sets ``in_progress`` + a
    lease and refuses if someone else holds it); to close a task prefer
    ``task_close``; to add dependencies use ``task_link``.

    Parameters
    ----------
    slug:
        The ``tk-`` slug of the task to update.
    title:
        New short label for the work.
    description:
        New full description. Replaces the existing text; it is not appended to.
    type:
        ``bug`` | ``feature`` | ``task`` | ``chore`` | ``epic``.
    status:
        ``open`` | ``in_progress`` | ``blocked`` | ``closed``. Two values have
        side effects: ``in_progress`` stamps a fresh ``claimed_at`` lease, and
        ``closed`` stamps ``closed_at`` **and unblocks this task's dependents**,
        exactly as ``task_close`` does. Prefer ``task_claim`` / ``task_close``
        for those two transitions — they carry the ownership checks this does
        not.
    priority:
        ``p0`` (highest) … ``p3``. Drives ``task_ready`` ordering.
    repo:
        Canonical repo URI to (re)assign this task to. Pass an explicit value to
        correct tasks that were created without proper repo context.
    assignee:
        Holder identity to reassign the task to. Prefer ``task_claim`` to take a
        task for yourself — it checks nobody else holds it, which this does not.
    project_slug:
        ``wp-`` slug of the WorkflowProject this task rolls up to.
    parent:
        ``tk-`` slug of the parent task/epic. Written as both the
        ``parent_slug`` field and the ``ParentOf`` edge in one commit, so a
        concurrent reader never sees the task parented one way and not the
        other. **Re-parenting does not retract the previous edge**: the field
        moves to the new parent while the old ``ParentOf`` remains, so a task
        re-parented this way is reachable from both. Call
        ``task_unlink(kind="parent")`` on the old pair first if the edge
        matters to you.
    external_uri:
        Reference URI — e.g. the GitHub issue or PR this task tracks.
    symbol_refs:
        Code-graph symbol ids (``repo#path::Name``) this task concerns.
        Replaces the existing list.
    tags:
        Free-form tags. Replaces the existing list rather than merging into it.
    """
    changes: dict = {}
    if title is not None:
        changes["title"] = title
    if description is not None:
        changes["description"] = description
    if type is not None:
        changes["type"] = type
    if priority is not None:
        changes["priority"] = priority
    if repo is not None:
        changes["repo"] = repo
    if assignee is not None:
        changes["assignee"] = assignee
    if project_slug is not None:
        changes["project_slug"] = project_slug
    if external_uri is not None:
        changes["external_uri"] = external_uri
    if symbol_refs is not None:
        changes["symbol_refs"] = symbol_refs
    if tags is not None:
        changes["tags"] = tags
    if status is not None:
        changes["status"] = status
        if status == "closed":
            changes["closed_at"] = now_iso()
        elif status == "in_progress":
            # Stamp a lease here too, not just in task_claim, so "started" has one
            # representation (see readiness.status_pickable's updated_at fallback
            # for stores/rows written before this existed).
            changes["claimed_at"] = now_iso()

    # The parent is one edit in two encodings — the `parent_slug` field and the
    # ParentOf edge — so it is one commit, not three. It used to update the
    # row, write the edge, then update the row AGAIN just to record
    # `parent_slug`; a reader between those commits saw a task parented one way
    # and not the other.
    extra_steps: list[_Step] = []
    if parent is not None:
        changes["parent_slug"] = parent
        extra_steps.append(
            ("mutations.gq", "link_parent_of", {"from": parent, "to": slug})
        )

    updated, _new_commit = _update_task(slug, changes, extra_steps=extra_steps)

    # Closing here must unblock dependents too, matching task_close.
    if status == "closed" and updated is not None:
        _unblock_dependents(updated.get("repo"))

    return updated


@_tool
def task_close(slug: str, resolution: str | None = None) -> dict | None:
    """
    Close a task: set status ``closed``, stamp ``closed_at``, record a resolution.

    Closing a blocker is what unblocks its dependents — they become visible to
    ``task_ready`` once every blocker is closed.

    Parameters
    ----------
    slug:
        The ``tk-`` slug to close.
    resolution:
        Short note on what was actually done. Worth writing: this is what a
        later reader sees when asking why the task ended, and closing without
        one leaves that unanswerable.
    """
    closed, _new_commit = _update_task(
        slug,
        {"status": "closed", "closed_at": now_iso(), "resolution": resolution},
    )
    if closed:
        _unblock_dependents(closed.get("repo"))
    return closed


@_tool
async def task_claim(
    slug: str,
    assignee: str | None = None,
    session_id: str | None = None,
    force: bool = False,
    ctx: Context | None = None,
) -> dict | None:
    """
    Claim a task for work: set it ``in_progress`` under ``assignee`` with a lease.

    The coordination primitive for parallel/multi-user agents — call it before
    starting a ready task so others see it is taken. Returns ``{"claimed": true,
    …}`` on success, or ``{"claimed": false, "reason": …}`` when the task is
    closed, still blocked, held by someone else (``"held"``/``"lost_race"``),
    or the graph is too busy to complete the CAS after retrying
    (``"contention"`` — safe and expected to retry). The lease (``claimed_at``)
    expires if the holder never closes/releases, making the task reclaimable
    (see ``task_ready``); re-calling renews it. Pass ``force`` to steal a live
    claim.

    BEST-EFFORT CAS — omnigraph 0.8.x exposes no conditional-write primitive, so
    the claim write cannot be made atomic at the store. Instead we surface the
    Lance optimistic-concurrency conflict (rather than masking it with a retry)
    and re-read + post-write-verify ownership, so a lost race reports
    ``{"claimed": false, "reason": "lost_race", "held_by": …}`` instead of
    silently clobbering the winner. The lease is the backstop for the residual
    simultaneous-post-read case. True atomic CAS awaits an upstream omnigraph
    conditional-write feature — see docs/adr/0003.

    Parameters
    ----------
    slug:
        The ``tk-`` slug to claim.
    assignee:
        Holder identity. Defaults to the calling user (the JWT's
        ``preferred_username`` when deployed, the configured author locally)
        qualified by ``session_id`` — see ``_claim_holder``. Two of one
        person's parallel sessions must not share a holder string, or the
        contention check passes and the second silently renews the first's
        lease. Pass an explicit id to override (a worker name, a CI job).
    session_id:
        The calling agent session's id, which qualifies the default holder.
        **Pass this when calling a deployed witan** — the server cannot infer
        it (no shared environment, and MCP 2026-07-28 carries no session
        state), and without it every one of your concurrent sessions claims
        under the same name. The CLI's remote proxy fills it in automatically;
        under local stdio the server falls back to its own
        ``$CLAUDE_SESSION_ID``, which it inherits from the agent.
    force:
        Steal the task even if another holder's lease is still valid.
    """
    # Best-effort CAS, not a hard lock: omnigraph has no conditional-write, so we
    # surface OCC conflicts and post-write-verify ownership rather than trusting
    # last-write-wins (see docs/adr/0003 and the claim loop below). On success
    # also upserts a CodeBranch for the checkout's repo+branch and links it
    # WorksOn this task (best-effort).
    holder = _claim_holder(assignee, session_id)
    rows = await _offload(client.read, "read.gq", "get_task", {"slug": slug})
    if not rows:
        return None
    task = rows[0]
    status = task.get("status")
    if status == "closed":
        return {"slug": slug, "claimed": False, "reason": "closed"}
    if status == "blocked":
        # A task can be stale-blocked (blocked_by is empty or all closed). Run the
        # unblock sweep before rejecting so stale status doesn't permanently prevent
        # claiming.
        await _offload(_unblock_dependents, task.get("repo"))
        rows = await _offload(client.read, "read.gq", "get_task", {"slug": slug})
        if not rows or rows[0].get("status") == "blocked":
            return {"slug": slug, "claimed": False, "reason": "blocked"}
        task = rows[0]
        status = task.get("status")

    current_holder = task.get("assignee")
    claimed_at = task.get("claimed_at")
    # Being unable to name the holder is not evidence there isn't one: a task
    # moved to in_progress via task_update (no task_claim call) has no assignee
    # and no claimed_at, but is still someone's work-in-progress until its lease
    # (falling back to updated_at, same as readiness.status_pickable) lapses.
    lease_started_at = claimed_at or task.get("updated_at")
    held = status == "in_progress" and not _lease_expired(lease_started_at)
    if held and current_holder != holder and not force:
        # Offer to steal instead of a flat refusal. Headless/unsupported clients
        # get the historical behavior (no steal); an explicit confirm proceeds as
        # if ``force`` had been passed.
        holder_desc = current_holder or _UNKNOWN_HOLDER
        stole = await elicit.confirm(
            ctx,
            f"Task {slug} is held by {holder_desc} (claimed {claimed_at}). "
            "Steal the claim?",
            default_when_unsupported=False,
            title="Steal claim?",
        )
        if not stole:
            return {
                "slug": slug,
                "claimed": False,
                "reason": "held",
                "held_by": holder_desc,
                "claimed_at": claimed_at,
                "remedy": _claim_remedy(slug, holder_desc, lease_started_at),
            }
        force = True

    now = now_iso()
    claim = {"status": "in_progress", "assignee": holder, "claimed_at": now}
    # The claim is a compare-and-swap: `conditional=True` states the branch head
    # the merged row was read at, so the write applies only while nothing else
    # has committed. On each surfaced conflict we re-read and either bail (a
    # rival won) or re-attempt. surface_conflict stays on for every attempt — a
    # consecutive conflict must never fall back to the blind-retry path, which
    # would re-apply the claim over whoever committed in the meantime.
    #
    # ★ WHAT CHANGED, AND WHAT DID NOT. Before omnigraph #470 there was no
    # conditional-write primitive at all, so this loop was read → write →
    # re-read → hope, with the post-write verification below as the only real
    # backstop. The precondition now makes a refusal AUTHORITATIVE: a 412 is the
    # store saying the write did not apply, rather than us inferring it from a
    # conflict that might have been someone else's.
    #
    # ★ THE VERIFICATION BELOW STAYS ANYWAY, and deliberately. It is what covers
    # the degraded path (a tier that supplies no graph_commit_id writes
    # unconditionally — see `_update_task`), and it is cheap next to a claim.
    # Removing it would make correctness depend on a server capability this code
    # cannot see from here.
    #
    # ★ EXPECT MORE CONFLICTS THAN CONTENTION. The precondition is the whole
    # branch head, not this row, so ANY concurrent write to the graph invalidates
    # it — a rival claim and an unrelated `memory_store` are indistinguishable.
    # That is why _CLAIM_MAX_ATTEMPTS exists and why a re-read that finds the
    # task still claimable retries rather than reporting a lost race.
    write_commit: str | None = None
    for attempt in range(_CLAIM_MAX_ATTEMPTS):
        try:
            _, write_commit = await _offload(
                _update_task, slug, claim, surface_conflict=True, conditional=True
            )
            break
        except OmnigraphConflict:
            # A concurrent writer committed between our read and our write.
            rows = await _offload(client.read, "read.gq", "get_task", {"slug": slug})
            fresh = rows[0] if rows else {}
            fresh_status = fresh.get("status")
            # ★ REVALIDATE CLAIMABILITY BEFORE EVER RETRYING, not just before
            # giving up. `_update_task`'s merge sets `status` from `claim`
            # unconditionally (see its docstring) — it does not care what the
            # fresh row it reads says — so a retry that goes ahead while the
            # task is now closed/blocked would silently resurrect it to
            # in_progress rather than conflict again. That window used to be
            # a bare network round-trip; the backoff below (and the wider
            # attempt budget) makes it wide enough to matter. Bail with the
            # real reason instead of looping back into a write that would
            # stomp it.
            if fresh_status == "closed":
                return {"slug": slug, "claimed": False, "reason": "closed"}
            if fresh_status == "blocked":
                return {"slug": slug, "claimed": False, "reason": "blocked"}
            rival = fresh.get("assignee")
            fresh_lease_started_at = fresh.get("claimed_at") or fresh.get("updated_at")
            if (
                fresh_status == "in_progress"
                and rival != holder
                and not _lease_expired(fresh_lease_started_at)
                and not force
            ):
                return {
                    "slug": slug,
                    "claimed": False,
                    "reason": "lost_race",
                    "held_by": rival or _UNKNOWN_HOLDER,
                    "claimed_at": fresh.get("claimed_at"),
                    "remedy": _claim_remedy(
                        slug, rival or _UNKNOWN_HOLDER, fresh_lease_started_at
                    ),
                }
            # The task itself still looks claimable, so the conflict came
            # from something else on the branch (an unrelated write, a rival
            # claim already released, a same-holder update, or — with
            # `force=True` — a still-live rival we intend to steal anyway).
            # The branch-head precondition does not tell us which; back off
            # and retry now that the manifest has advanced.
            if attempt + 1 == _CLAIM_MAX_ATTEMPTS:
                # Exhausted the budget without ever seeing a rival hold the
                # task or the task become closed/blocked. Report it as a
                # retryable condition rather than leaking the raw omnigraph
                # "write authority ... changed during preparation" text to
                # the caller (tk-task-claim-exhausts-its-3-attempt-no-backoff-
                # cas-674414).
                logger.warning(
                    "witan.task_claim.contention_exhausted",
                    task_slug=slug,
                    holder=holder,
                    attempts=_CLAIM_MAX_ATTEMPTS,
                )
                return {
                    "slug": slug,
                    "claimed": False,
                    "reason": "contention",
                    "remedy": (
                        f"{_CLAIM_MAX_ATTEMPTS} claim attempts each hit a "
                        "write conflict on the graph without the task itself "
                        "becoming closed, blocked, or held by a rival — most "
                        "likely heavy write load elsewhere on the graph, "
                        "though the branch-head precondition cannot fully "
                        "rule out a same-task race. Retry task_claim."
                    ),
                }
            await anyio.sleep(_claim_backoff(attempt + 1))

    # Post-write verification: with no store-level CAS, a rival's claim could
    # still have landed last. Re-read and confirm we actually hold it before
    # reporting success, so at most one caller ever sees claimed=True.
    #
    # ★ UNCONSTRAINED, NOT PINNED TO OUR OWN WRITE. An earlier version of this
    # fix pinned the read to `write_commit` (our own write's `graph_commit_id`)
    # and shipped in agent-kit#248 before review caught the flaw: a read fixed
    # to our own commit can only ever show what WE wrote — it is structurally
    # blind to a legitimate LATER write (a rival's `force` claim, a concurrent
    # `task_update`), which is exactly the clobber this verification exists to
    # catch (see `test_claim_post_write_verification_catches_last_writer`).
    # Pinning made that test pass for the wrong reason: it runs over the CLI
    # locally, where `write_commit` is always `None`, so the pinned branch
    # never actually ran.
    #
    # ★ BUT AN UNCONSTRAINED READ HAS NO FRESHNESS GUARANTEE EITHER, which is
    # the bug tk-mutual-exclusion-violated-2-of-8-racers-both-got-52b3dd
    # actually proved: on 2026-08-18, one racer's verification read returned a
    # snapshot 2 SECONDS OLDER than a rival's write that had already
    # committed — by its OWN reported `graph_commit_id`, so the read was not
    # lying about what it served, it genuinely served stale data.
    #
    # So: read unconstrained (to stay able to see a later clobber), but do not
    # TRUST it until its own reported commit has caught up to (or passed)
    # `write_commit` — omnigraph's commit ids are ULIDs
    # (docs/user/concepts/storage.md upstream), lexicographically sortable by
    # creation time, so a plain string comparison is a valid "at least as new"
    # check. A read that is still behind ours is retried rather than trusted;
    # `write_commit is None` (the degraded path: a tier that supplied no
    # `graph_commit_id`, or the CLI transport, which never does) skips the
    # catch-up check entirely, since there is nothing to catch up to.
    rows, verify_commit = await _offload(
        client.read_with_commit, "read.gq", "get_task", {"slug": slug}
    )
    verify_attempts = 1
    if write_commit is not None:
        while verify_attempts < _VERIFY_CAUGHT_UP_MAX_ATTEMPTS and (
            verify_commit is None or verify_commit < write_commit
        ):
            await anyio.sleep(_VERIFY_CAUGHT_UP_BACKOFF_SECONDS)
            rows, verify_commit = await _offload(
                client.read_with_commit, "read.gq", "get_task", {"slug": slug}
            )
            verify_attempts += 1
    winner = rows[0].get("assignee") if rows else None
    caught_up = write_commit is None or (
        verify_commit is not None and verify_commit >= write_commit
    )
    logger.info(
        "witan.task_claim.verify",
        task_slug=slug,
        holder=holder,
        winner_seen=winner,
        verify_graph_commit_id=verify_commit,
        verify_attempts=verify_attempts,
        caught_up=caught_up,
        claim_granted=winner == holder or force,
    )
    if winner != holder and not force:
        winner_claimed_at = rows[0].get("claimed_at") if rows else None
        return {
            "slug": slug,
            "claimed": False,
            "reason": "lost_race",
            "held_by": winner or _UNKNOWN_HOLDER,
            "claimed_at": winner_claimed_at,
            "remedy": _claim_remedy(slug, winner or _UNKNOWN_HOLDER, winner_claimed_at),
        }
    await _offload(_track_code_branch, repo_module.detect(), task_slug=slug)
    return {
        "slug": slug,
        "claimed": True,
        "assignee": holder,
        "claimed_at": now,
        "stole": bool(held and current_holder != holder),
    }


@_tool
def task_release(
    slug: str,
    assignee: str | None = None,
    session_id: str | None = None,
    status: TaskStatus = "open",
    force: bool = False,
) -> dict | None:
    """
    Release a claim: clear the assignee/lease and return the task to ``open``.

    Call when stepping away from an unfinished task so others can pick it up
    (closing a finished task — use ``task_close`` — also ends the claim). Refuses
    if the task is held by a different ``assignee`` unless ``force`` is set.

    Parameters
    ----------
    slug:
        The ``tk-`` slug to release.
    assignee:
        Holder identity releasing the task. Defaults to the calling user, same
        resolution as ``task_claim``'s ``assignee``. The held-by check compares
        *identities*, ignoring the ``#<session>`` qualifier, so you can release a
        claim one of your own other sessions took; another person's still needs
        ``force``.
    session_id:
        The calling agent session's id, same resolution and same reason as
        ``task_claim``'s. Only affects the holder string this call is compared
        *as*; since the comparison is identity-level, omitting it against a
        deployed server is harmless here in a way it is not for ``task_claim``.
    status:
        Status to return the task to (default ``open``).
    force:
        Release even if held by a different assignee.
    """
    holder = _claim_holder(assignee, session_id)
    rows = client.read("read.gq", "get_task", {"slug": slug})
    if not rows:
        return None
    current_holder = rows[0].get("assignee")
    # Identity-level match, so you can release a claim your *other* session took
    # (same person, different `#<session>` qualifier) without reaching for force.
    # Another person's claim still needs it.
    if current_holder and not _same_person(current_holder, holder) and not force:
        return {
            "slug": slug,
            "released": False,
            "held_by": current_holder,
            "remedy": (
                f"{slug} is held by {current_holder}, not {holder}. Release it "
                f"anyway with `witan task release {slug} --force`."
            ),
        }

    _update_task(slug, {"status": status, "assignee": None, "claimed_at": None})
    return {"slug": slug, "released": True, "status": status}


@_tool
def task_ready(
    repo: str | None = None,
    project_slug: str | None = None,
    assignee: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """
    Return ready-to-work tasks: pickable tasks whose blockers are all closed.

    A task is ready when its status is ``open``/``blocked`` (nobody is on it yet
    and it is not closed), OR ``in_progress`` with an expired lease (the holder
    likely abandoned it — see ``readiness.status_pickable``), AND every task in
    its ``blocked_by`` list is closed. A returned ``in_progress`` task is
    therefore a reclaim, not fresh work — check ``assignee``/``claimed_at``
    (falling back to ``updated_at`` when ``claimed_at`` is null, e.g. a legacy
    row) before starting it. This is the core coordination primitive — call it
    to pick the next actionable item without manual triage. Results are
    ordered by priority (``p0`` first).

    Parameters
    ----------
    repo:
        Repo scoping — see instructions.
    project_slug:
        Restrict to a single WorkflowProject.
    assignee:
        Restrict to a single owner (or pass to find your own ready work).
    limit:
        Maximum tasks to return. Defaults to 20.
    """
    if project_slug:
        rows = client.read(
            "read.gq", "list_tasks_by_project", {"project_slug": project_slug}
        )
    else:
        detected = repo_module.detect(override=repo)
        if detected:
            repo_rows = client.read("read.gq", "list_tasks_by_repo", {"repo": detected})
            all_rows = client.read("read.gq", "list_unscoped_tasks", {})
            seen = {r["slug"] for r in repo_rows}
            unscoped = [
                r for r in all_rows if not r.get("repo") and r["slug"] not in seen
            ]
            rows = repo_rows + unscoped
        else:
            rows = client.read("read.gq", "list_unscoped_tasks", {})

    status_by_slug = {r["slug"]: r.get("status") for r in rows}

    def blocker_status(blocker_slug: str) -> str:
        if blocker_slug in status_by_slug:
            return status_by_slug[blocker_slug] or "open"
        fetched = client.read("read.gq", "get_task", {"slug": blocker_slug})
        # A blocker that no longer exists does not hold anything back.
        return fetched[0].get("status", "closed") if fetched else "closed"

    ready = [
        r
        for r in rows
        if readiness.is_ready(r, blocker_status)
        and _holder_matches(r.get("assignee"), assignee)
    ]
    ready.sort(key=lambda r: _PRIORITY_ORDER.get(r.get("priority"), 9))
    return ready[:limit]


@_tool
def task_link(from_slug: str, to_slug: str, kind: TaskLinkKind) -> dict:
    """
    Link two tasks (or a task to a memory).

    The meaning of ``from``/``to`` depends on ``kind``:
    - ``blocks``          — ``from`` is the blocker, ``to`` is the blocked task.
      This is the only way to set a task's ``blocked_by`` after creation
      (``task_update`` does not).
    - ``parent``          — ``from`` is the parent/epic, ``to`` is the child.
      Sets the same edge as ``task_update(parent=…)``; prefer ``task_update``.
    - ``discovered_from`` — ``from`` is the new task, ``to`` is the source it came from.
    - ``addresses``       — ``from`` is the task, ``to`` is a Memory slug it addresses.

    For ``blocks`` and ``parent`` the denormalized ``blocked_by`` / ``parent_slug``
    fields on the affected task are kept in sync so ``task_ready`` stays correct.

    Reversible: ``task_unlink`` removes any of these, including one recorded in
    the wrong direction.

    Parameters
    ----------
    from_slug:
        The ``tk-`` slug the edge points **from**. Its meaning depends on
        ``kind`` — see above; for ``blocks`` this is the blocker, for ``parent``
        the epic, for ``discovered_from`` the newly-found task.
    to_slug:
        The slug the edge points **to** — the blocked task, the child, the task
        the work was discovered during, or (for ``addresses``) a ``Memory``
        slug.
    kind:
        ``blocks`` | ``parent`` | ``discovered_from`` | ``addresses``. Getting
        the direction wrong is the common mistake here, and it matters: for
        ``blocks`` it decides which task ``task_ready`` withholds.
    """
    if kind == "blocks":
        # The edge always lands; the denormalized `blocked_by`/`status` sync
        # only when there's something new to sync. Reading `to_slug` FIRST
        # (rather than after, as this used to) is what lets the two land in
        # ONE commit via `extra_steps` when both apply — order doesn't change
        # what's read, since the edge write doesn't touch `to_slug`'s row.
        link_step: _Step = (
            "mutations.gq",
            "link_blocks",
            {"from": from_slug, "to": to_slug},
        )
        blocked = client.read("read.gq", "get_task", {"slug": to_slug})
        changes = None
        if blocked:
            existing = blocked[0].get("blocked_by") or []
            if from_slug not in existing:
                changes = {"blocked_by": [*existing, from_slug]}
                if blocked[0].get("status") == "open":
                    blocker = client.read("read.gq", "get_task", {"slug": from_slug})
                    if blocker and blocker[0].get("status") != "closed":
                        changes["status"] = "blocked"
        if changes is not None:
            _update_task(to_slug, changes, extra_steps=[link_step])
        else:
            client.change(*link_step)
    elif kind == "parent":
        # Edge + `parent_slug` sync are one logical edit, always both — the
        # same `extra_steps` mechanism `_update_task`'s own `parent_slug`
        # writes already use for their CodeBranch edges.
        _update_task(
            to_slug,
            {"parent_slug": from_slug},
            extra_steps=[
                ("mutations.gq", "link_parent_of", {"from": from_slug, "to": to_slug})
            ],
        )
    elif kind == "discovered_from":
        client.change(
            "mutations.gq", "link_discovered_from", {"from": from_slug, "to": to_slug}
        )
    elif kind == "addresses":
        client.change(
            "mutations.gq", "link_addresses", {"from": from_slug, "to": to_slug}
        )

    return {"from": from_slug, "to": to_slug, "kind": kind}


# Per edge kind: the read queries listing each endpoint's edges, and the
# mutations deleting by one endpoint or re-inserting a survivor.
_EDGE_OPS: dict[str, dict[str, str]] = {
    "blocks": {
        "from_q": "blocks_from_slugs",
        "to_q": "blocks_to_slugs",
        "del_by_to": "unlink_blocks_by_to",
        "del_by_from": "unlink_blocks_by_from",
        "link": "link_blocks",
    },
    "parent": {
        "from_q": "parent_of_from_slugs",
        "to_q": "parent_of_to_slugs",
        "del_by_to": "unlink_parent_of_by_to",
        "del_by_from": "unlink_parent_of_by_from",
        "link": "link_parent_of",
    },
    "discovered_from": {
        "from_q": "discovered_from_from_slugs",
        "to_q": "discovered_from_to_slugs",
        "del_by_to": "unlink_discovered_from_by_to",
        "del_by_from": "unlink_discovered_from_by_from",
        "link": "link_discovered_from",
    },
    "addresses": {
        "from_q": "addresses_from_slugs",
        "to_q": "addresses_to_slugs",
        "del_by_to": "unlink_addresses_by_to",
        "del_by_from": "unlink_addresses_by_from",
        "link": "link_addresses",
    },
    "project_blocks": {
        "from_q": "project_blocks_from_slugs",
        "to_q": "project_blocks_to_slugs",
        "del_by_to": "unlink_project_blocks_by_to",
        "del_by_from": "unlink_project_blocks_by_from",
        "link": "link_project_blocks",
    },
}


def _unlink_edge(kind: str, from_slug: str, to_slug: str) -> bool:
    """Remove one ``from -> to`` edge, leaving every other edge of that kind.

    Returns whether the edge existed. A no-op (and no mutation at all) when it
    did not, so calling this twice is safe.

    An edge delete accepts exactly ONE predicate — ``delete E where to = $x``
    or ``where from = $x``, never both (mutations.gq explains). So an exact
    single-edge removal is: delete every edge on one endpoint, then put back
    the ones that were not the target. Two things keep that honest:

    - The side with FEWER edges is chosen, so the fewest possible edges are
      disturbed. In the common case one side has exactly this edge and nothing
      is re-inserted at all — a single delete, nothing to restore.
    - Nothing is deleted unless the target edge is confirmed to exist first.

    NOT ATOMIC, and on a shared server that matters: between the delete and the
    re-inserts, a concurrent reader sees the survivors missing, and a crash
    would drop them. omnigraph has no conditional write to close this — see
    tk-omnigraph-conditional-write-cas-precondition-on--94155f. The exposure is
    bounded by choosing the smaller side, and is nil for the single-edge case
    that motivated this. ``task_link`` already does an unguarded
    read-modify-write on ``blocked_by``, so this adds no new class of risk.
    """
    ops = _EDGE_OPS[kind]
    into = [r["slug"] for r in client.read("read.gq", ops["from_q"], {"to": to_slug})]
    out = [r["slug"] for r in client.read("read.gq", ops["to_q"], {"from": from_slug})]
    if from_slug not in into or to_slug not in out:
        return False

    if len(out) <= len(into):
        # Delete everything `from_slug` points at, restore all but `to_slug`.
        client.change("mutations.gq", ops["del_by_from"], {"from": from_slug})
        survivors = [(from_slug, other) for other in out if other != to_slug]
    else:
        # Delete everything pointing at `to_slug`, restore all but `from_slug`.
        client.change("mutations.gq", ops["del_by_to"], {"to": to_slug})
        survivors = [(other, to_slug) for other in into if other != from_slug]

    for src, dst in survivors:
        client.change("mutations.gq", ops["link"], {"from": src, "to": dst})
    return True


@_tool
def task_unlink(from_slug: str, to_slug: str, kind: TaskLinkKind) -> dict:
    """
    Remove a link between two tasks (or a task and a memory) — the inverse of
    ``task_link``, with the same ``from``/``to`` meanings.

    Use it when a link was recorded the wrong way round or against the wrong
    slug. Removing a ``blocks`` link is how a task wrongly marked blocked
    becomes ready again.

    For ``blocks`` and ``parent`` the denormalized ``blocked_by`` /
    ``parent_slug`` fields are updated to match, so ``task_ready`` stays
    correct. Unblocking a task whose remaining blockers are all closed returns
    it from ``blocked`` to ``open`` — the mirror of what ``task_link`` does.

    Returns ``{"from", "to", "kind", "removed"}``. ``removed`` is ``False``
    when the edge was not there, which is not an error: calling this twice, or
    on a link that never existed, is a safe no-op.

    Parameters
    ----------
    from_slug:
        The ``tk-`` slug the edge points **from** — the same direction it was
        written with. Pass the endpoints as ``task_link`` received them, not
        reversed.
    to_slug:
        The slug the edge points **to**, or a ``Memory`` slug for
        ``kind="addresses"``.
    kind:
        Which edge to remove: ``blocks`` | ``parent`` | ``discovered_from`` |
        ``addresses``.
    """
    removed = _unlink_edge(kind, from_slug, to_slug)

    if kind == "blocks":
        blocked = client.read("read.gq", "get_task", {"slug": to_slug})
        if blocked:
            existing = blocked[0].get("blocked_by") or []
            if from_slug in existing:
                remaining = [s for s in existing if s != from_slug]
                changes: dict = {"blocked_by": remaining or None}
                # Only clear `blocked` once nothing open is still holding it.
                if blocked[0].get("status") == "blocked":
                    still_held = any(
                        (rows := client.read("read.gq", "get_task", {"slug": s}))
                        and rows[0].get("status") != "closed"
                        for s in remaining
                    )
                    if not still_held:
                        changes["status"] = "open"
                _update_task(to_slug, changes)
    elif kind == "parent" and removed:
        child = client.read("read.gq", "get_task", {"slug": to_slug})
        if child and child[0].get("parent_slug") == from_slug:
            _update_task(to_slug, {"parent_slug": None})

    return {"from": from_slug, "to": to_slug, "kind": kind, "removed": removed}


@_tool
def symbol_context(symbol_id: str) -> dict:
    """
    Memories and tasks attached to a code symbol (direction: symbol → work).

    The reverse of ``memory_symbols``: given a symbol id, returns the memories and
    tasks whose ``symbol_refs`` include it — "what lessons and open tasks concern
    this function?". Call it after locating a symbol with the witan-code ``code_*``
    tools, before editing.

    ``symbol_id`` has the form ``<repo>#<path/to/file.py>::<QualifiedName>``; the
    repo prefix scopes the lookup, or the current repo when the id has no ``#``.

    Parameters
    ----------
    symbol_id:
        The symbol to look up, as returned in the ``symbol_id`` field of the
        witan-code ``code_*`` tools.
    """
    return _context_for_symbol(symbol_id)


def _context_for_symbol(symbol_id: str) -> dict:
    repo = symbol_id.split("#", 1)[0] if "#" in symbol_id else repo_module.detect()

    if repo:
        mem_rows = client.read("read.gq", "memories_by_repo", {"repo": repo})
        task_rows = client.read("read.gq", "tasks_by_repo_refs", {"repo": repo})
    else:
        mem_rows = client.read("read.gq", "memories_with_refs", {})
        task_rows = client.read("read.gq", "tasks_with_refs", {})

    memories = [m for m in mem_rows if symbol_id in (m.get("symbol_refs") or [])]
    tasks = [t for t in task_rows if symbol_id in (t.get("symbol_refs") or [])]
    return {"symbol_id": symbol_id, "memories": memories, "tasks": tasks}


# ── Hard memory↔symbol / memory↔contract links (spec §4) ──────────

ContractKind = Literal["env_var", "endpoint", "package", "service"]


def _code_server():
    """The witan-code server module if installed/reachable, else None.

    Cross-store resolution (symbol definitions, bridge bindings) is best-effort:
    witan-code is an optional sibling package, so callers degrade to raw refs
    and empty bindings when it is absent — never an edge into another store.

    The result (module or None) is cached on the function: a failed import isn't
    cached in ``sys.modules``, so without this every call would re-scan
    ``sys.path``. A broad ``except`` keeps a witan-code that's installed but
    import-broken from raising into a memory operation.
    """
    if not hasattr(_code_server, "_cached"):
        try:
            from witan_code import server as code_server  # noqa: PLC0415

            _code_server._cached = code_server
        except Exception:  # noqa: BLE001 — optional dependency; degrade to None
            # Info, not warning: witan-code genuinely is optional, so absence is
            # a supported configuration. But it is cached forever after this, so
            # if it failed for a *broken* install rather than an absent one,
            # this line is the only chance to find out.
            logger.info("witan.code_server.unavailable", exc_info=True)
            _code_server._cached = None
    return _code_server._cached


@_tool
def memory_for_contract(key_norm: str, kind: ContractKind | None = None) -> dict:
    """
    What do we know about a contract (env_var / endpoint / package / service)?

    Resolves the ``Topic{kind:"contract", name:key_norm}`` anchor and walks
    ``Tagged`` to the memories about it (a single Layer-1 traversal, cross-repo by
    nature), then — best-effort — asks witan-code for the bridge bindings that
    share the same ``key_norm`` so callers can pivot to the code that produces or
    consumes it. The two halves are joined in Python on the shared key, never an
    edge across stores.

    Tag a memory to a contract first with
    ``memory_link(memory_slug, "<key_norm>:contract", "tagged")`` (``from`` is the
    memory's slug).

    Parameters
    ----------
    key_norm:
        The normalised contract key (e.g. ``DATABASE_URL`` or
        ``GET /api/v1/courses/``).
    kind:
        The bridge binding kind (``env_var`` / ``endpoint`` / ``package`` /
        ``service``) used to look up bindings. Omit to skip the bridge lookup.

    Returns ``{"key_norm", "kind", "memories": [...], "bindings": {...}}``.
    """
    rows = client.read(
        "read.gq", "topic_by_name_kind", {"name": key_norm, "kind": "contract"}
    )
    memories = (
        client.read("read.gq", "memories_for_topic", {"topic_slug": rows[0]["slug"]})
        if rows
        else []
    )

    bindings: dict = {"providers": [], "consumers": []}
    code = _code_server()
    if code is not None and kind is not None:
        try:
            bindings = {
                "providers": code.code_interface_providers(kind, key_norm),
                "consumers": code.code_interface_consumers(kind, key_norm),
            }
        except Exception:  # noqa: BLE001 — cross-store lookup is best-effort
            # Info: the contract answer is still returned, just without its
            # code bindings — a quietly thinner result that otherwise looks
            # like "this contract has no providers or consumers".
            logger.info(
                "witan.contract.bindings_lookup_failed",
                kind=kind,
                key=key_norm,
                exc_info=True,
            )

    return {
        "key_norm": key_norm,
        "kind": kind,
        "memories": memories,
        "bindings": bindings,
    }


@_tool
def memory_symbols(slug: str) -> dict:
    """
    Code symbols a memory concerns (direction: memory → symbols).

    The reverse of ``symbol_context``: returns each of the memory's ``symbol_refs``,
    enriched with the live definition when witan-code is reachable, or the raw ref
    strings otherwise.

    Parameters
    ----------
    slug:
        The memory whose ``symbol_refs`` to resolve. An unknown slug returns an
        empty ``symbols`` list rather than raising.
    """
    node = memory_get(slug)
    if node is None:
        return {"slug": slug, "symbols": []}

    code = _code_server()
    symbols: list[dict] = []
    for ref in node.get("symbol_refs") or []:
        entry = {"symbol_ref": ref}
        # Only a well-formed symbol id (repo#path::Name) is worth resolving;
        # skip the cross-store call for anything without the :: delimiter.
        if code is not None and "::" in ref:
            try:
                name = ref.split("::")[-1]
                repo = ref.split("#", 1)[0] if "#" in ref else None
                defs = code.code_find_definition(name, repo)
                if defs:
                    entry["definition"] = defs
            except Exception:  # noqa: BLE001 — best-effort enrichment
                # Debug: one symbol failing to resolve a definition is common
                # (a renamed or removed symbol still referenced by a memory),
                # and this runs per symbol, so anything louder would be noisy.
                logger.debug(
                    "witan.symbol.definition_lookup_failed", ref=ref, exc_info=True
                )
        symbols.append(entry)
    return {"slug": slug, "symbols": symbols}


# ── Graph-aware recall (spec §8) ──────────────────────────────────


def _expand_neighbors(slug: str) -> set[str]:
    """One-hop memory neighbours of ``slug``: along AppliesTo/RelatedTo, topic
    siblings (Tagged), and provenance siblings (same producing session).

    Each relation is a single conjunctive query — topic_siblings and
    provenance_siblings join memory→topic/session→memory in one roundtrip rather
    than fanning out per topic/session."""
    out: set[str] = set()
    for query in (
        "applies_to_targets",
        "applies_to_sources",
        "related_out",
        "related_in",
        "topic_siblings",
        "provenance_siblings",
    ):
        out.update(r["slug"] for r in client.read("read.gq", query, {"slug": slug}))
    out.discard(slug)
    return out


@_tool
def recall(
    query: str | None = None,
    symbol_id: str | None = None,
    task: str | None = None,
    topic: str | None = None,
    repo: str | None = None,
    kind: MemoryKind | None = None,
    hops: int = 1,
    limit: int = 20,
    include_superseded: bool = False,
) -> dict:
    """
    Graph-aware contextual recall — the composition of every other memory tool.

    Seeds from any combination of ``query`` (BM25), ``symbol_id``
    (``symbol_context``), ``task`` (memories it Addresses + memories sharing
    its symbol_refs), and ``topic`` (memories tagged to it). Expands ``hops``
    (default 1, capped at 2) along AppliesTo/RelatedTo edges, topic siblings, and
    provenance siblings; prunes superseded memories (unless
    ``include_superseded``); flags Contradicts pairs; and re-ranks with the
    composite score minus a per-hop distance penalty so seeds outrank neighbours.

    With no edges in the graph the result equals ``memory_search`` — expansion is
    additive, never lossy. Embeddings are deferred behind ``WITAN_EMBED_ENABLED``
    (default off); ``recall`` works with BM25 only and needs no embedding provider.

    Returns ``{"memories": [...ranked...], "contradictions": [...], "seeds": {...}}``.

    Parameters
    ----------
    query:
        Free-text BM25 query, matched against ``content`` and ``title``.
    symbol_id:
        A code-graph symbol id (``repo#path::Name``) to seed from — the memories
        and tasks whose ``symbol_refs`` include it. Use this before editing a
        symbol: "what do we already know about this function?"
    task:
        A ``tk-`` slug to seed from — the memories it ``Addresses`` plus those
        sharing its ``symbol_refs``. The one-call way to load context for a task
        you are about to start.
    topic:
        A Topic slug (``tp-...``) or a ``name:kind`` spec (e.g. ``uv:topic``,
        ``DATABASE_URL:contract``) to seed from. Topics are a cross-repo join
        surface, so this seed in particular can pull in other repositories.
    repo:
        Repo scoping — see instructions. Applies to the ``query`` seed only;
        symbol, task, and topic seeds resolve wherever they live.
    kind:
        Restrict the ``query`` seed to one memory kind: ``pattern``,
        ``project_fact``, ``lesson``, or ``agent_context``.
    hops:
        How far to expand from the seeds along ``AppliesTo`` / ``RelatedTo``
        edges, topic siblings, and provenance siblings. Clamped to 0–2. ``0``
        disables expansion and returns the seeds alone; ``1`` (the default) is
        almost always right, since each extra hop widens results faster than it
        deepens them.
    limit:
        Maximum memories to return after re-ranking.
    include_superseded:
        When ``True``, keep memories that a newer memory ``Supersedes``. Default
        ``False`` hides them, which is what makes recall return current
        knowledge rather than its history.
    """
    hops = max(0, min(hops, 2))
    now = datetime.now(timezone.utc)

    # ── Seed ──────────────────────────────────────────────────────
    seed_rank: dict[str, int] = {}  # query-seed BM25 position (norm proxy)
    seeds: dict[str, list[str]] = {"query": [], "symbol": [], "task": [], "topic": []}
    candidates: dict[str, int] = {}  # slug → min hop distance

    def add_seed(slug: str, bucket: str) -> None:
        seeds[bucket].append(slug)
        candidates.setdefault(slug, 0)

    if query:
        for i, r in enumerate(_search_rows(query, repo, kind)):
            seed_rank.setdefault(r["slug"], i)
            add_seed(r["slug"], "query")
    if symbol_id:
        for m in _context_for_symbol(symbol_id)["memories"]:
            add_seed(m["slug"], "symbol")
    if task:
        for m in client.read("read.gq", "addressed_memories", {"task_slug": task}):
            add_seed(m["slug"], "task")
        task_rows = client.read("read.gq", "get_task", {"slug": task})
        for sym in (task_rows[0].get("symbol_refs") if task_rows else None) or []:
            for m in _context_for_symbol(sym)["memories"]:
                add_seed(m["slug"], "task")
    if topic:
        topic_slug = _lookup_topic_slug(topic)
        if topic_slug:
            for m in client.read(
                "read.gq", "memories_for_topic", {"topic_slug": topic_slug}
            ):
                add_seed(m["slug"], "topic")

    # ── Expand ────────────────────────────────────────────────────
    frontier = set(candidates)
    for distance in range(1, hops + 1):
        nxt: set[str] = set()
        for slug in frontier:
            for neighbor in _expand_neighbors(slug):
                if neighbor not in candidates:
                    candidates[neighbor] = distance
                    nxt.add(neighbor)
        frontier = nxt
        if not frontier:
            break

    # No seeds matched (and nothing to expand from) — skip the global edge scan.
    if not candidates:
        return {"memories": [], "contradictions": [], "seeds": {}}

    # ── Prune + score ─────────────────────────────────────────────
    edge_index = _edge_index()
    if not include_superseded:
        candidates = {
            s: h for s, h in candidates.items() if s not in edge_index["superseded"]
        }

    n_query = len(seed_rank)

    def norm_bm25(slug: str) -> float:
        if slug not in seed_rank:
            return 0.0
        return 1.0 if n_query <= 1 else (n_query - 1 - seed_rank[slug]) / (n_query - 1)

    scored: list[tuple[float, dict]] = []
    for slug, hop in candidates.items():
        node = memory_get(slug)
        if node is None:
            continue
        base = _score(
            norm_bm25=norm_bm25(slug),
            age_days=_age_days(node.get("updated_at") or node.get("created_at"), now),
            corroboration=edge_index["corroboration"].get(slug, 0),
            confidence=node.get("confidence"),
            is_superseded=slug in edge_index["superseded"],
            is_contradicted=slug in edge_index["contradicted"],
            rank_cfg=rank_cfg,
        )
        scored.append((base - rank_cfg.w_hop * hop, node))
    scored.sort(key=lambda t: -t[0])
    returned = [node for _, node in scored][:limit]

    # ── Contradictions among the RETURNED memories ────────────────
    # Only over the limited result set, so every pair references a memory the
    # caller actually receives.
    returned_slugs = {n["slug"] for n in returned}
    contradictions: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    for slug in returned_slugs:
        for other in client.read("read.gq", "contradicts_out", {"slug": slug}):
            if other["slug"] not in returned_slugs:
                continue
            pair = tuple(sorted((slug, other["slug"])))
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                contradictions.append({"a": pair[0], "b": pair[1]})

    return {
        "memories": returned,
        "contradictions": contradictions,
        "seeds": {k: v for k, v in seeds.items() if v},
    }
