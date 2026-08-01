"""Where a repo's code graph lives, and how to reach it.

A code graph is EITHER a local ``<slug>.omni`` directory under ``code_dir`` OR
a graph on the deployed omnigraph-server (``--server <url> --graph <id>``, the
id from :func:`config.graph_id`). Which one is a deployment question, answered
once by ``code_server`` (:attr:`config.Config.code_server`), so every caller
resolves a :class:`StoreRef` and asks *it* rather than branching on config.

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
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import config as cfg_module
from .graph import OmnigraphClient

# Graph registration changes only when provisioning does (a Pulumi deploy), but
# `_client_for_repo` asks "does this store exist" on every MCP tool call — and
# on the cluster that answer costs a subprocess, where locally it was a stat.
# One short-lived listing per process amortizes a burst of tool calls without
# pinning a stale answer past a deploy.
_GRAPHS_TTL = 30.0
_graphs_cache: dict[str, tuple[float, frozenset[str]]] = {}


class ClusterGraphMissing(RuntimeError):
    """A cluster graph witan-code needs is not registered with the server."""


@dataclass(frozen=True)
class StoreRef:
    """One code graph's address — local directory or cluster graph.

    ``uri`` is what :class:`~witan_code.graph.OmnigraphClient` takes as its
    ``graph_uri``: a local path, or the server's base URL. ``graph_id`` is set
    only for a cluster graph, where it selects which of the server's N graphs
    this is.
    """

    uri: str
    graph_id: str | None = None
    token: str | None = None

    @property
    def is_remote(self) -> bool:
        """Whether this graph lives on an omnigraph-server.

        Same rule the client itself applies (http(s) → ``--server/--graph``),
        so a ref and the client built from it never disagree about which one
        of them is talking to a cluster.
        """
        return self.uri.startswith(("http://", "https://"))

    @property
    def local_path(self) -> Path | None:
        """The store directory, or ``None`` for a cluster graph.

        ``None`` is the explicit answer for every filesystem-shaped question a
        caller might have. It is not an error to ask — it is an error to
        assume.
        """
        return None if self.is_remote else Path(self.uri)

    def __str__(self) -> str:
        return f"{self.uri} (graph {self.graph_id})" if self.is_remote else self.uri

    def client(
        self,
        config: cfg_module.Config | None = None,
        branch: str | None = None,
    ) -> OmnigraphClient:
        cfg = config or cfg_module.load()
        return OmnigraphClient(
            self.uri,
            cfg.queries_dir,
            token=self.token,
            branch=branch,
            graph_id=self.graph_id,
        )

    def exists(self) -> bool:
        """Whether this graph is there to be read.

        Locally that is the store directory; on the cluster it is registration
        with the server (``omnigraph graphs list``), which is what provisioning
        creates. A registered-but-empty graph exists — callers distinguish that
        with :func:`file_count`, exactly as they do for a freshly-init'd local
        store.
        """
        if not self.is_remote:
            return Path(self.uri).exists()
        return self.graph_id in cluster_graphs(self.uri, self.token)

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

    Empty on any failure — an unreachable server reads as "nothing indexed"
    for listings, the same degradation an unreadable ``code_dir`` already got,
    rather than taking down every ``code_*`` tool. ``ensure_store`` is the one
    caller that turns "not registered" into a hard error, because that is the
    one case where continuing would silently write nowhere.
    """
    cached = _graphs_cache.get(server_url)
    if cached is not None and time.monotonic() - cached[0] < _GRAPHS_TTL:
        return cached[1]
    env = dict(os.environ)
    if token:
        env["OMNIGRAPH_SERVER_BEARER_TOKEN"] = token
    try:
        res = subprocess.run(
            [_binary(), "graphs", "list", "--server", server_url, "--json"],
            capture_output=True,
            text=True,
            env=env,
        )
        found = _parse_graph_ids(res.stdout) if res.returncode == 0 else []
    except (OSError, RuntimeError):
        found = []
    graphs = frozenset(found)
    _graphs_cache[server_url] = (time.monotonic(), graphs)
    return graphs


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
        value = row.get("id") or row.get("name") if isinstance(row, dict) else row
        if isinstance(value, str):
            out.append(value)
    return out


# ── Resolution ────────────────────────────────────────────────────────────────


def store_for_repo(slug: str, config: cfg_module.Config | None = None) -> StoreRef:
    """Where ``slug``'s code graph lives. Does not create or verify anything."""
    cfg = config or cfg_module.load()
    if cfg.code_server:
        return StoreRef(cfg.code_server, cfg_module.graph_id(slug), cfg.code_token)
    return StoreRef(str(cfg_module.store_path(slug, cfg.code_dir)))


def bridge_store(config: cfg_module.Config | None = None) -> StoreRef:
    """Where the shared cross-repo bridge graph lives. Creates nothing."""
    cfg = config or cfg_module.load()
    if cfg.code_server:
        return StoreRef(cfg.code_server, cfg_module.BRIDGE_GRAPH_ID, cfg.code_token)
    return StoreRef(str(cfg_module.bridge_store_path(cfg.code_dir)))


def store_exists(slug: str, config: cfg_module.Config | None = None) -> bool:
    return store_for_repo(slug, config).exists()


def ensure_store(slug: str, config: cfg_module.Config | None = None) -> StoreRef:
    """Resolve ``slug``'s code graph, ready to be written to.

    Locally that means creating it: ``omnigraph init --schema <schema>
    <store>``, mirroring install.sh.

    On the cluster it means CHECKING it. A client cannot create a cluster graph
    — ``init`` is a direct-storage command that rejects ``--server``, and the
    set of graphs is declared by provisioning (ol-infrastructure
    ``applications/omnigraph/data_tier.py``), which applies their schema too.
    So the client's job is to fail loudly when the graph it is about to write
    is not one the cluster knows: the alternative is a run that turns thousands
    of symbols into one error per subprocess and reports success having written
    nothing.
    """
    cfg = config or cfg_module.load()
    if cfg.code_server:
        return _verify_cluster_graph(
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
        _schema_apply(binary, cfg.schema_file, store)
    else:
        # Only re-apply when the schema file has changed since the last apply.
        # schema apply is additive/idempotent but spawns a subprocess on every
        # PostToolUse reindex; the mtime sidecar avoids the cost on hot paths.
        _schema_apply_if_changed(binary, cfg.schema_file, store)

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
    if cfg.code_server:
        return _verify_cluster_graph(bridge_store(cfg), "the bridge graph", cfg)

    store = cfg_module.bridge_store_path(cfg.code_dir)
    binary = _binary()
    if store.exists():
        # Pick up additive schema changes (new nodes/fields) on existing
        # stores; the mtime stamp keeps hot reindex paths subprocess-free.
        _schema_apply_if_changed(binary, cfg.bridge_schema_file, store)
        return StoreRef(str(store))

    store.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [binary, "init", "--schema", str(cfg.bridge_schema_file), str(store)],
        check=True,
        capture_output=True,
        text=True,
    )
    _schema_apply(binary, cfg.bridge_schema_file, store)
    return StoreRef(str(store))


def _verify_cluster_graph(ref: StoreRef, what: str, cfg: cfg_module.Config) -> StoreRef:
    if ref.exists():
        return ref
    known = ", ".join(sorted(cluster_graphs(ref.uri, ref.token))) or "(none)"
    unset = (
        f"code_server on target [{cfg.target_name}]"
        if cfg.target_name
        else "WITAN_CODE_SERVER / code_server"
    )
    raise ClusterGraphMissing(
        f"{what} is not registered with the omnigraph-server at {ref.uri}: "
        f"expected graph {ref.graph_id!r}; the server serves {known}.\n"
        "Cluster graphs are declared by provisioning (ol-infrastructure "
        "applications/omnigraph/data_tier.py), not created by this client — "
        f"add the graph there, or unset {unset} to index locally."
    )


def per_repo_stores(config: cfg_module.Config | None = None) -> list[StoreRef]:
    """Every indexed per-repo code graph, sorted. Excludes the bridge graph.

    On the cluster this is the server's graph registry filtered to the
    ``code-`` prefix — which is *declared* coverage, so a provisioned but
    never-indexed repo shows up here with 0 files. That is the honest answer to
    the question these listings exist to settle ("would a cross-repo query have
    found this?"): the graph is queryable and empty, not absent.
    """
    cfg = config or cfg_module.load()
    if cfg.code_server:
        return [
            StoreRef(cfg.code_server, gid, cfg.code_token)
            for gid in sorted(cluster_graphs(cfg.code_server, cfg.code_token))
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


def _schema_stamp(store: Path) -> Path:
    return store.parent / f"{store.name}.schema_mtime"


def _schema_apply(binary: str, schema_file: Path, store: Path) -> None:
    res = subprocess.run(
        [binary, "schema", "apply", "--schema", str(schema_file), str(store)],
        capture_output=True,
        text=True,
    )
    if res.returncode == 0:
        _schema_stamp(store).write_text(str(schema_file.stat().st_mtime))


def _schema_apply_if_changed(binary: str, schema_file: Path, store: Path) -> None:
    stamp = _schema_stamp(store)
    current_mtime = str(schema_file.stat().st_mtime)
    if stamp.exists() and stamp.read_text().strip() == current_mtime:
        return
    _schema_apply(binary, schema_file, store)


def repo_for_store(ref: StoreRef, config: cfg_module.Config | None = None) -> str:
    """Canonical repo URI for a code graph.

    Locally: the exact sidecar if present, else a best-effort reconstruction
    from the (lossily) sanitized filename.

    On the cluster there is nowhere to put a sidecar and :func:`config.graph_id`
    does not invert (it collapses runs of non-alphanumerics and may truncate),
    so the graph is asked what it holds — one row, ``indexed_repo``. A graph
    with no files has no answer and falls back to its id, which is at least
    unambiguous even though it is not a URI.
    """
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
