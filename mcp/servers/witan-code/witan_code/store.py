"""Where a repo's code graph lives, and how to reach it.

A code graph is a local ``<slug>.omni`` directory under ``code_dir``, or a
graph on the deployed omnigraph-server — reached either directly (``--server
<url> --graph <id>``, the id from :func:`config.graph_id`) or through the
deployed witan MCP tier, which is the only one of the two that is reachable
from outside the cluster. All three are deployment questions, answered once by
``code_server`` / ``code_transport`` (:mod:`witan_code.config`), so every
caller resolves a :class:`StoreRef` and asks *it* rather than branching on
config.

The distinction is not cosmetic: a cluster graph has no directory, so the
operations that were only ever a filesystem walk — "does it exist", "how big
is it", "what else is indexed", "which repo is this" — each need a real answer
on the cluster or an explicit refusal to give one. :class:`StoreRef` gives them
one place to be answered rather than a dozen ``.exists()`` calls that silently
mean "no" for a graph that is very much there.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from witan_core.omnigraph import (
    BEARER_TOKEN_ENV_VAR,
    schema_apply,
    schema_apply_if_changed,
)

from . import config as cfg_module
from .graph import OmnigraphClient

# Graph registration changes only when provisioning does (a Pulumi deploy), but
# `_client_for_repo` asks "does this store exist" on every MCP tool call — and
# on the cluster that answer costs a subprocess, where locally it was a stat.
# One short-lived listing per process amortizes a burst of tool calls without
# pinning a stale answer past a deploy.
_GRAPHS_TTL = 30.0
_graphs_cache: dict[str, tuple[float, frozenset[str]]] = {}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class ClusterGraphMissing(RuntimeError):
    """A cluster graph witan-code needs is not served by the server."""


class ClusterUnreachable(RuntimeError):
    """The omnigraph-server could not be asked about a graph at all.

    Distinct from :class:`ClusterGraphMissing` on purpose. "The server says
    that graph does not exist" and "I could not reach the server" are the same
    failed probe, and collapsing them sends you to check provisioning for what
    is really a bad token or a closed port — which is exactly what happened the
    first time this ran against the live CI cluster.
    """


@dataclass(frozen=True)
class StoreRef:
    """One code graph's address — local directory or cluster graph.

    ``uri`` is what :class:`~witan_code.graph.OmnigraphClient` takes as its
    ``graph_uri``: a local path, or the server's base URL. ``graph_id`` is set
    only for a cluster graph addressed directly, where it selects which of the
    server's N graphs this is.

    ``via_mcp`` marks the third form: a cluster graph reached through the witan
    MCP tier, whose ``uri`` is that endpoint and whose value is the *name* the
    deployment resolves — a canonical repo URI, or
    :data:`config.BRIDGE_GRAPH_ID` for the bridge, which has no repo of its
    own. A per-repo graph travels as the repo URI rather than as a graph id
    because the deployment maps repo to graph with its own configuration; a
    client that sent an id would be asserting one it derived, and
    :func:`config.graph_id` does not invert.
    """

    uri: str
    graph_id: str | None = None
    token: str | None = None
    via_mcp: str | None = None

    @property
    def is_remote(self) -> bool:
        """Whether this graph is a shared one rather than a directory here.

        Same rule the client itself applies (http(s) → ``--server/--graph``),
        so a ref and the client built from it never disagree about which one
        of them is talking to a cluster. A graph reached through the MCP tier
        is shared by construction, which is what every caller of this is
        really asking — the write guard included.
        """
        return self.via_mcp is not None or self.uri.startswith(("http://", "https://"))

    @property
    def local_path(self) -> Path | None:
        """The store directory, or ``None`` for a cluster graph.

        ``None`` is the explicit answer for every filesystem-shaped question a
        caller might have. It is not an error to ask — it is an error to
        assume.
        """
        return None if self.is_remote else Path(self.uri)

    def __str__(self) -> str:
        # A remote ref built from a bare `--store <url>` has no graph id yet, so
        # the suffix is conditional: "… (graph None)" in a user-facing refusal
        # reads like a bug in the tool rather than a URL missing its graph.
        if self.via_mcp:
            return f"{self.via_mcp} (via {self.uri})"
        if self.is_remote and self.graph_id:
            return f"{self.uri} (graph {self.graph_id})"
        return self.uri

    def client(
        self,
        config: cfg_module.Config | None = None,
        branch: str | None = None,
        connect_retry: bool = True,
    ):
        cfg = config or cfg_module.load()
        if self.via_mcp is not None:
            # No connect_retry counterpart: the MCP tier's own session handles
            # transport, and a tool call has no omnigraph-CLI retry budget to
            # opt out of.
            from .remote.store import RemoteStoreClient  # noqa: PLC0415

            return RemoteStoreClient(self.via_mcp, mcp_session(cfg), branch=branch)
        return OmnigraphClient(
            self.uri,
            cfg.queries_dir,
            token=self.token,
            branch=branch,
            graph_id=self.graph_id,
            connect_retry=connect_retry,
        )

    def exists(self, config: cfg_module.Config | None = None) -> bool:
        """Whether this graph is there to be read.

        Locally that is the store directory; on the cluster it is whether the
        server serves this graph id, which is what provisioning creates. A
        registered-but-empty graph exists — callers distinguish that with
        :func:`file_count`, exactly as they do for a freshly-init'd local store.

        Asked the same way in both cluster forms: list the graph's own branches
        (see :func:`probe_cluster_graph`). A server that cannot be reached
        answers ``False`` here, since a read path has nothing better to do with
        it; the write path calls :func:`probe_cluster_graph` directly so it can
        tell the two apart — and, unlike this, waits for a restarting server
        rather than calling it absent. Hence ``connect_retry=False``: a read
        path that degrades to "no" should say so now, not in 150s.
        """
        if not self.is_remote:
            return Path(self.uri).exists()
        try:
            self.client(config, connect_retry=False).list_branches()
        except Exception:  # noqa: BLE001 — a read path degrades to "no"
            return False
        return True

    def stats(self) -> tuple[int | None, float | None]:
        """``(total_bytes, latest mtime)``, or ``(None, None)`` on the cluster.

        Both are properties of a directory on this machine. A client of a
        shared graph has no directory to walk and no business reporting the
        server's disk, so it reports neither rather than a plausible zero —
        callers render "?" (see ``code_indexed_repos``).
        """
        if self.is_remote:
            return None, None
        return dir_stats(Path(self.uri))


def cluster_graphs(server_url: str, token: str | None = None) -> frozenset[str]:
    """Graph ids registered with the omnigraph-server at ``server_url``.

    ENUMERATION ONLY, and best-effort: nothing on a write path may depend on
    this. ``graphs list`` is a *server-scoped* action, and on omnigraph 0.8.1 it
    fails against the deployed cluster two independent ways:

    1. The management surface is closed by default — "server-scoped actions
       require an explicit cluster policy bundle applied with ``omnigraph
       cluster apply``" — in every runtime state, ``--unauthenticated``
       included. The deployed cluster.yaml declares no ``policies:`` block yet,
       and adding one is a hard cutover to authenticated-everything (the server
       refuses to boot with a policy but no bearer tokens), gated behind the
       per-environment token rollout.
    2. Even with the bundle applied and a token that *is* granted ``graph_list``,
       the 0.8.1 CLI then refuses to print the listing it just fetched: "server
       scope '<url>' has N graphs: pass --graph <id> to select one". There is no
       working invocation for a multi-graph server (``--uri`` is retired: "a
       remote graph must be addressed with ``--server <url>``").

    So on the cluster this answers the empty set today, and the callers that
    remain are the ones a blank listing merely degrades — see
    :func:`safe_cluster_graphs`. Asking whether one *known* graph is served is a
    different, graph-scoped question with a working answer:
    :func:`probe_cluster_graph`.

    Raises :class:`ClusterUnreachable` when the server could not be asked, so
    that an auth failure or a closed port cannot masquerade as a server that
    genuinely has no graphs.

    A successful-but-empty answer is cached; a failure is not, so a transient
    outage doesn't pin an error for the whole TTL.
    """
    cached = _graphs_cache.get(server_url)
    if cached is not None and time.monotonic() - cached[0] < _GRAPHS_TTL:
        return cached[1]
    try:
        res = subprocess.run(
            [_binary(), "graphs", "list", "--server", server_url, "--json"],
            capture_output=True,
            text=True,
            env=_token_env(token),
        )
    except (OSError, RuntimeError) as exc:
        raise ClusterUnreachable(f"could not run omnigraph graphs list: {exc}") from exc
    if res.returncode != 0:
        raise ClusterUnreachable(
            f"omnigraph-server at {server_url} could not be listed "
            f"(exit {res.returncode}): {_clean(res.stderr)}"
        )
    graphs = frozenset(_parse_graph_ids(res.stdout))
    _graphs_cache[server_url] = (time.monotonic(), graphs)
    return graphs


def safe_cluster_graphs(server_url: str, token: str | None = None) -> frozenset[str]:
    """:func:`cluster_graphs`, degrading to empty when the server can't be asked.

    For the listing paths (``code_indexed_repos``, the prompt-hook coverage
    line), which already treat an unreadable ``code_dir`` as "nothing
    indexed" and must not take down a whole MCP tool call over it.
    """
    try:
        return cluster_graphs(server_url, token)
    except ClusterUnreachable:
        return frozenset()


def _token_env(token: str | None) -> dict[str, str]:
    """Environment for the ``graphs list`` subprocess, carrying ``token``.

    Uses :data:`witan_core.omnigraph.BEARER_TOKEN_ENV_VAR` rather than spelling
    the name again — this is the one omnigraph call witan-code makes without
    going through :class:`OmnigraphClient`, since listing a server's graphs is
    not scoped to any one of them, so it is the one place the shared client's
    token handling has to be repeated. Repeating the *name* is what put the
    two out of step before.

    No local-store branch here, unlike ``_execute``'s: this only ever runs
    against a server URL, so there is no address that could receive a token
    with nothing to do with it.
    """
    env = dict(os.environ)
    if token:
        env[BEARER_TOKEN_ENV_VAR] = token
    return env


def _clean(stderr: str) -> str:
    """omnigraph's error text without ANSI codes or the Rust backtrace boilerplate."""
    return _ANSI_RE.sub("", stderr).split("Location:")[0].strip()


def reset_graph_cache() -> None:
    """Drop the memoized graph listings (tests, and after a provisioning change)."""
    _graphs_cache.clear()


def _parse_graph_ids(payload: str) -> list[str]:
    """Graph ids out of ``omnigraph graphs list --json``.

    Reads both the bare-list and ``{"graphs": [...]}`` envelopes, and both a
    plain id and a ``{"id": …}``/``{"name": …}`` record — the same shape
    tolerance ``list_branches`` applies, for the same reason: the envelope has
    already changed once across omnigraph releases.
    """
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return []
    rows = parsed.get("graphs", parsed) if isinstance(parsed, dict) else parsed
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        # Parenthesized for the reader, not the parser: `or` binds tighter than
        # the conditional, so the `.get`s were already confined to the dict
        # branch. A review read it the other way round, which is reason enough.
        value = (row.get("id") or row.get("name")) if isinstance(row, dict) else row
        if isinstance(value, str):
            out.append(value)
    return out


# ── Resolution ────────────────────────────────────────────────────────────────


def _endpoint(cfg: cfg_module.Config):
    """The deployed witan MCP endpoint this process writes cluster graphs through.

    Resolved against ``cfg``'s own target, not by re-running target selection:
    ``cfg`` may have come from an explicit ``load(target=…)``, and re-selecting
    would fall back to ``WITAN_TARGET``/auto-detection and could answer with a
    *different* deployment than the one whose ``code_transport`` sent us here —
    writing to the wrong endpoint, or refusing with a "none configured" message
    naming a target that has one.

    A ``code_transport = "mcp"`` with no endpoint configured is a misconfigured
    client, not an absent graph, so it raises rather than degrading to a local
    store — which would silently index into a directory nobody reads.
    """
    remote = cfg_module.load_remote_config(cfg.target_name)
    if remote is None:
        where = f"target [{cfg.target_name}]" if cfg.target_name else "WITAN_REMOTE_URL"
        raise ValueError(
            f"code_transport is {cfg_module.CODE_TRANSPORT_MCP!r}, which reaches "
            "the cluster's code graphs through the deployed witan MCP endpoint "
            f"— but none is configured. Set remote_url (and oidc_issuer) on "
            f"{where}, or use code_transport="
            f"{cfg_module.CODE_TRANSPORT_DIRECT!r} with a reachable code_server."
        )
    return remote


def mcp_session(config: cfg_module.Config | None = None):
    """The MCP session this process reaches cluster graphs through.

    One per endpoint, held open for the process (:mod:`witan_code.remote.store`).
    """
    cfg = config or cfg_module.load()
    remote = _endpoint(cfg)
    from .remote.oidc import default_token_provider  # noqa: PLC0415
    from .remote.store import session_for  # noqa: PLC0415

    return session_for(remote.url, default_token_provider(remote))


def _via_mcp(cfg: cfg_module.Config) -> bool:
    return cfg.code_transport == cfg_module.CODE_TRANSPORT_MCP


def store_for_repo(slug: str, config: cfg_module.Config | None = None) -> StoreRef:
    """Where ``slug``'s code graph lives. Does not create or verify anything."""
    cfg = config or cfg_module.load()
    if _via_mcp(cfg):
        return StoreRef(_endpoint(cfg).url, via_mcp=slug)
    if cfg.code_server:
        return StoreRef(cfg.code_server, cfg_module.graph_id(slug), cfg.code_token)
    return StoreRef(str(cfg_module.store_path(slug, cfg.code_dir)))


def bridge_store(config: cfg_module.Config | None = None) -> StoreRef:
    """Where the shared cross-repo bridge graph lives. Creates nothing."""
    cfg = config or cfg_module.load()
    if _via_mcp(cfg):
        return StoreRef(_endpoint(cfg).url, via_mcp=cfg_module.BRIDGE_GRAPH_ID)
    if cfg.code_server:
        return StoreRef(cfg.code_server, cfg_module.BRIDGE_GRAPH_ID, cfg.code_token)
    return StoreRef(str(cfg_module.bridge_store_path(cfg.code_dir)))


def store_exists(slug: str, config: cfg_module.Config | None = None) -> bool:
    return store_for_repo(slug, config).exists(config)


def ensure_store(slug: str, config: cfg_module.Config | None = None) -> StoreRef:
    """Resolve ``slug``'s code graph, ready to be written to.

    Locally that means creating it: ``omnigraph init --schema <schema>
    <store>``, mirroring install.sh.

    On the cluster it means CHECKING it — :func:`probe_cluster_graph`, the same
    graph-scoped probe for both cluster forms (direct ``--server/--graph`` and
    through the MCP tier). Provisioning declares the graphs and applies their
    schema; the client's job is only to fail loudly when the graph it is about
    to write is not one the cluster serves.
    """
    cfg = config or cfg_module.load()
    if _via_mcp(cfg) or cfg.code_server:
        return probe_cluster_graph(
            store_for_repo(slug, cfg), f"{slug}'s code graph", cfg
        )

    store = cfg_module.store_path(slug, cfg.code_dir)
    binary = _binary()

    if not store.exists():
        store.parent.mkdir(parents=True, exist_ok=True)
        # ASSUMPTION: `omnigraph init --schema <file> <store>` is the per-store
        # init form, matching witan/install.sh.
        subprocess.run(
            [binary, "init", "--schema", str(cfg.schema_file), str(store)],
            check=True,
            capture_output=True,
            text=True,
        )
        # Force apply on first creation — stamp will be written below.
        schema_apply(binary, cfg.schema_file, store)
    else:
        # Only re-apply when the schema file has changed since the last apply.
        # schema apply is additive/idempotent but spawns a subprocess on every
        # PostToolUse reindex; the mtime sidecar avoids the cost on hot paths.
        schema_apply_if_changed(binary, cfg.schema_file, store)

    # Record the canonical repo URI in a sidecar so listings can show it even for
    # a 0-file store (sanitize_slug is lossy — its `_` collapse isn't reversible).
    repo_sidecar(store).write_text(slug)
    return StoreRef(str(store))


def ensure_bridge_store(config: cfg_module.Config | None = None) -> StoreRef:
    """Resolve the shared bridge graph, initialising it from bridge-schema.pg.

    Mirrors :func:`ensure_store` — including its cluster behavior, where the
    bridge graph is declared by provisioning under the fixed
    :data:`config.BRIDGE_GRAPH_ID` and this only verifies it. ``schema apply``
    builds the FTS index on ``key_norm`` that ``search_bindings`` needs.
    """
    cfg = config or cfg_module.load()
    if _via_mcp(cfg) or cfg.code_server:
        return probe_cluster_graph(bridge_store(cfg), "the bridge graph", cfg)

    store = cfg_module.bridge_store_path(cfg.code_dir)
    binary = _binary()
    if store.exists():
        # Pick up additive schema changes (new nodes/fields) on existing
        # stores; the mtime stamp keeps hot reindex paths subprocess-free.
        schema_apply_if_changed(binary, cfg.bridge_schema_file, store)
        return StoreRef(str(store))

    store.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [binary, "init", "--schema", str(cfg.bridge_schema_file), str(store)],
        check=True,
        capture_output=True,
        text=True,
    )
    schema_apply(binary, cfg.bridge_schema_file, store)
    return StoreRef(str(store))


_NOT_FOUND_RE = re.compile(r"graph '[^']*' not found", re.IGNORECASE)


def probe_cluster_graph(ref: StoreRef, what: str, cfg: cfg_module.Config) -> StoreRef:
    """Check that the cluster serves ``ref`` before a write starts.

    The client cannot create a cluster graph — ``init`` is a direct-storage
    command that rejects ``--server``, and the set of graphs is declared by
    provisioning — so a run that proceeds past a missing one turns thousands of
    records into one error per subprocess and reports success having written
    nothing.

    Asks a GRAPH-scoped question (``branch list --graph <id>``, the cheapest
    one that has to reach the store to be answered) rather than the
    server-scoped ``graphs list``. That is not a detail: ``graphs list`` is a
    management-surface action that no ordinary client can use against the
    deployed cluster (see :func:`cluster_graphs` for the two ways it fails),
    and depending on it here is what left every environment's indexer failing
    before it parsed a single file. Listing one graph's branches needs only
    ``read`` on that graph, which every actor the v1 Cedar bundle grants — CI,
    users, service and admin alike — already holds.

    It also inherits :class:`~witan_core.omnigraph.OmnigraphClient`'s
    connect-retry budget, so a server mid-restart is ridden out rather than
    counted as an absent graph.

    Still distinguishes the two failures the previous ``graphs list`` path
    distinguished, because they send you to different places: the server says
    ``graph '<id>' not found`` for one it does not serve, and anything else
    (a closed port, a rejected token) is :class:`ClusterUnreachable`.
    """
    try:
        ref.client(cfg).list_branches()
    except Exception as exc:  # noqa: BLE001 — re-raised as one of two kinds
        if not _NOT_FOUND_RE.search(str(exc)):
            raise ClusterUnreachable(
                f"{what} at {ref} could not be read: {exc}"
            ) from exc
        unset = (
            f"code_server on target [{cfg.target_name}]"
            if cfg.target_name
            else "WITAN_CODE_SERVER / code_server"
        )
        raise ClusterGraphMissing(
            f"{what} is not served by the omnigraph-server at {ref}.\n"
            "Cluster graphs are declared by provisioning (ol-infrastructure "
            "applications/omnigraph/data_tier.py), not created by this client — "
            f"add the graph there, or unset {unset} to index locally."
        ) from exc
    return ref


def per_repo_stores(config: cfg_module.Config | None = None) -> list[StoreRef]:
    """Every indexed per-repo code graph, sorted. Excludes the bridge graph.

    On the cluster this is the server's graph registry filtered to the
    ``code-`` prefix — which is *declared* coverage, so a provisioned but
    never-indexed repo shows up here with 0 files. That is the honest answer to
    the question these listings exist to settle ("would a cross-repo query have
    found this?"): the graph is queryable and empty, not absent.

    Enumerating is the one thing that genuinely needs the server-scoped
    ``graphs list``, so on the direct path it is empty until that surface works
    (:func:`cluster_graphs`). A blank *listing* is a degraded answer, not a
    broken write — which is exactly why nothing on the write path may ask this
    question. Through the MCP tier the deployment answers it instead, and does.
    """
    cfg = config or cfg_module.load()
    if _via_mcp(cfg):
        # The deployment holds the registry and is the only side that can map a
        # graph back to the repo it holds, so the listing arrives already
        # resolved — no `repo_for_store` query per graph from here.
        try:
            repos = mcp_session(cfg).call("code_store_graphs")
        except Exception:  # noqa: BLE001 — a listing degrades, same as above
            return []
        return [StoreRef(_endpoint(cfg).url, via_mcp=repo) for repo in sorted(repos)]
    if cfg.code_server:
        return [
            StoreRef(cfg.code_server, gid, cfg.code_token)
            for gid in sorted(safe_cluster_graphs(cfg.code_server, cfg.code_token))
            if gid.startswith(cfg_module.CODE_GRAPH_PREFIX)
            and gid != cfg_module.BRIDGE_GRAPH_ID
        ]
    if not cfg.code_dir.is_dir():
        return []
    return [
        StoreRef(str(p))
        for p in sorted(cfg.code_dir.glob("*.omni"))
        if p.name != cfg_module.BRIDGE_STORE_NAME
    ]


def repo_sidecar(store: Path) -> Path:
    """Sidecar file next to a LOCAL store holding its canonical repo URI."""
    return store.parent / f"{store.name}.repo"


def repo_for_store(ref: StoreRef, config: cfg_module.Config | None = None) -> str:
    """Canonical repo URI for a code graph.

    Locally: the exact sidecar if present, else a best-effort reconstruction
    from the (lossily) sanitized filename.

    On the cluster there is nowhere to put a sidecar and :func:`config.graph_id`
    does not invert (it collapses runs of non-alphanumerics and may truncate),
    so the graph is asked what it holds — one row, ``indexed_repo``. A graph
    with no files has no answer and falls back to its id, which is at least
    unambiguous even though it is not a URI.

    A ref reached through the MCP tier already carries the repo URI: it is what
    the deployment was asked to resolve, so there is nothing to ask back.
    """
    if ref.via_mcp is not None:
        return ref.via_mcp
    if ref.is_remote:
        try:
            rows = ref.client(config).read("code_read.gq", "indexed_repo", {})
        except Exception:  # noqa: BLE001 — a listing must not fail on one graph
            rows = []
        value = next(iter(rows[0].values()), None) if rows else None
        return value if isinstance(value, str) and value else (ref.graph_id or ref.uri)

    store = Path(ref.uri)
    sidecar = repo_sidecar(store)
    if sidecar.exists():
        return sidecar.read_text().strip()
    return repo_from_stem(store.stem)


def repo_from_stem(stem: str) -> str:
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


def file_count(ref: StoreRef, config: cfg_module.Config | None = None) -> int | None:
    """How many files ``ref`` has indexed, or None if it can't be read.

    Counts in the engine (``count_files``) rather than materializing a row per
    file: this runs per store in ``code_indexed_repos`` and on every prompt via
    the UserPromptSubmit hook, and the bulk read it replaced would also have
    undercounted a store past all_file_hashes' 1,000,000-row cap.
    """
    try:
        rows = ref.client(config).read("code_read.gq", "count_files", {})
    except Exception:  # noqa: BLE001 — degrade gracefully, a listing isn't critical
        return None
    if not rows:
        return 0
    # Read positionally: the column takes the match variable's name on a
    # populated store but is "?" on an empty one (see code_read.gq).
    return next(iter(rows[0].values()), 0)


def dir_stats(path: Path) -> tuple[int, float]:
    """Return (total_bytes, latest mtime as an epoch) in a single directory walk.

    LOCAL stores only — callers that may be looking at a cluster graph go
    through :meth:`StoreRef.stats`.

    The mtime stays an epoch rather than a formatted string so a caller reading
    a *remote* store's stats renders it in its own timezone, not the server's.

    ``os.walk`` rather than ``Path.rglob`` — a Lance store is thousands of
    fragment files, and this runs per store in ``code_indexed_repos`` plus on
    every prompt via the UserPromptSubmit hook, so the per-entry ``Path``
    construction is worth avoiding. Neither form follows directory symlinks.
    """
    total = 0
    latest = path.stat().st_mtime
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                # A broken symlink (which the previous is_file() check skipped),
                # or a file that vanished mid-walk — an index running alongside
                # this rewrites fragments constantly.
                continue
            total += st.st_size
            latest = max(latest, st.st_mtime)
    return total, latest


def _binary() -> str:
    return OmnigraphClient._find_binary()
