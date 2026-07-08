import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastmcp import Context, FastMCP

from . import config as cfg_module
from . import elicit
from . import readiness
from . import repo as repo_module
from . import scan
from . import session_state
from .graph import OmnigraphClient, OmnigraphConflict, _is_storage_version_mismatch

# ── Startup ───────────────────────────────────────────────────────

_SCHEMA_FILE = Path(__file__).parent.parent / "schema" / "schema.pg"


def _ensure_graph(graph_uri: str) -> None:
    """Initialise the local graph if it does not yet exist.

    No-op for remote (http/s3) URIs — those are assumed to be managed
    externally. Local stores are created and schema-applied automatically
    so `witan serve` and `witan <cmd>` work on a fresh machine without a
    separate install step.
    """
    if graph_uri.startswith(("http://", "https://", "s3://")):
        return
    store = Path(graph_uri).expanduser()
    if store.exists():
        return
    binary = OmnigraphClient._find_binary()
    store.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [binary, "init", "--schema", str(_SCHEMA_FILE), str(store)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [binary, "schema", "apply", "--schema", str(_SCHEMA_FILE), str(store)],
        check=True,
        capture_output=True,
        text=True,
    )


cfg = cfg_module.load()
rank_cfg = cfg_module.load_rank_config()
scan_cfg = cfg_module.load_scan_config()
_ensure_graph(cfg.graph_uri)
client = OmnigraphClient(
    cfg.graph_uri,
    cfg.queries_dir,
    cfg.graph_token,
    guard=scan.write_guard_from_config(scan_cfg),
)


def apply_schema() -> dict:
    """Apply the bundled ``schema.pg`` to the configured store (idempotent).

    Reconciles an EXISTING store with the current schema — ``_ensure_graph`` only
    schema-applies when first *creating* a store, so additive changes (new
    nodes/edges/fields) never reach an already-created store without this.

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
        "Updating memory: to replace an outdated memory, store the new one then "
        'memory_link(from_slug=<new>, to_slug=<old>, kind="supersedes") — the old '
        "one is hidden from default reads but kept (include_superseded=True to "
        "see it).\n\n"
        "Errors: a lookup that finds nothing returns null/empty, never raises; an "
        "invalid-but-well-formed mutation (self-link, self-block, claim "
        "contention) returns a status object with a reason; only malformed input "
        "raises."
    ),
)

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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Advisory-claim lease lives in ``readiness`` (shared with the context hook so
# the injected "Ready Tasks" list and ``task_ready`` agree). Re-exported under
# the historical names for the in-module call sites.
_CLAIM_LEASE_SECONDS = readiness.CLAIM_LEASE_SECONDS
_lease_expired = readiness.lease_expired

# Bounded re-tries for the best-effort CAS claim loop: on each surfaced
# optimistic-concurrency conflict we re-read and either bail (a rival won) or
# re-attempt the claim. Small, since a claim conflicting this many times in a row
# without a rival taking it is pathological.
_CLAIM_MAX_ATTEMPTS = 3


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


def _upsert_topic(name: str, kind: str) -> tuple[str, bool]:
    """Return the Topic slug for (name, kind), creating it if absent.

    The second element is ``True`` when the node was newly inserted — lets callers
    (e.g. ``migrate_topics``) count creations without a second ``get_topic`` read.
    """
    slug = _topic_slug(kind, name)
    if client.read("read.gq", "get_topic", {"slug": slug}):
        return slug, False
    client.change(
        "mutations.gq",
        "insert_topic",
        {"slug": slug, "name": name, "kind": kind, "created_at": _now_iso()},
    )
    return slug, True


def _resolve_topic(ref: str) -> str | None:
    """Resolve a topic reference to a slug, creating the Topic if needed.

    ``ref`` is either an existing Topic slug (``tp-...``) or a ``name:kind`` spec
    (e.g. ``cryptography:topic`` or ``GET /api/v1/courses/:contract``). Returns the
    slug, or ``None`` when ``ref`` is a slug that does not resolve to a Topic.
    """
    # Only a tp- slug can resolve directly; skip the read for a name:kind spec.
    if ref.startswith("tp-") and client.read("read.gq", "get_topic", {"slug": ref}):
        return ref
    name, sep, kind = ref.rpartition(":")
    if sep and kind in ("topic", "contract", "symbol", "entity") and name:
        return _upsert_topic(name, kind)[0]
    return None


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


def _tag_memory(memory_slug: str, name: str, kind: str) -> str:
    """Upsert a Topic and link the memory to it. Returns the topic slug."""
    topic_slug, _ = _upsert_topic(name, kind)
    client.change(
        "mutations.gq", "link_tagged", {"from": memory_slug, "to": topic_slug}
    )
    return topic_slug


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
    for row in rows:
        slug = row["slug"]
        existing = {
            t["slug"]
            for t in client.read("read.gq", "topics_for_memory", {"slug": slug})
        }
        for tag in dict.fromkeys(t for t in (row.get("tags") or []) if t.strip()):
            topic_slug = _topic_slug("topic", tag)
            if topic_slug not in seen_topics:
                _, created = _upsert_topic(tag, "topic")
                if created:
                    topics_created += 1
                seen_topics.add(topic_slug)
            if topic_slug not in existing:
                client.change(
                    "mutations.gq",
                    "link_tagged",
                    {"from": slug, "to": topic_slug},
                )
                edges_created += 1
    return {
        "memories_scanned": len(rows),
        "topics_created": topics_created,
        "edges_created": edges_created,
    }


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


def _find_pre_upgrade_binary(current_binary: str) -> str | None:
    """First ``omnigraph`` on PATH that isn't the binary witan is currently
    using — a candidate for whatever wrote the store before witan's own
    bundled binary moved on to a newer release."""
    current_real = Path(current_binary).resolve()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry) / "omnigraph"
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        try:
            if candidate.resolve() == current_real:
                continue
        except OSError:
            continue
        return str(candidate)
    return None


def _run_omnigraph(cmd: list[str], *, label: str, stdout=None) -> str:
    """Run an omnigraph subcommand.

    When ``stdout`` is an open file, output streams straight there (so a
    large ``export`` never sits fully buffered in memory) and ``""`` is
    returned; otherwise stdout is captured and returned as a string.
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
    full rebuild+swap, so a concurrent witan write can't race the migration."""
    lock_path = Path(f"{store}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")  # noqa: SIM115 — released by the caller
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    return fh


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

    old = old_binary or _find_pre_upgrade_binary(new_binary)
    if old is None:
        raise RuntimeError(
            "The store was written by an older, incompatible omnigraph "
            "on-disk format, and no other `omnigraph` binary was found on "
            "PATH to export it. Pass the path to the pre-upgrade binary that "
            "last wrote this store."
        )
    old_ok, old_out = _snapshot(old, store)
    if not old_ok:
        raise RuntimeError(
            f"The candidate old binary {old!r} can't read the store either:\n"
            f"{old_out.strip()}"
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
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        lock_fh.close()

    return {
        "migrated": True,
        "store": str(store_path),
        "backup": str(backup_path),
        "old_binary": old,
        "new_binary": new_binary,
        "verify": verify_out.strip(),
    }


# ── Composite re-rank (spec §7) ───────────────────────────────────


def _search_rows(query: str, repo: str | None, kind: str | None) -> list[dict]:
    """BM25 candidate rows in score-desc order (the seed step for §3.5 / §8)."""
    detected = repo_module.detect(override=repo)
    if detected and kind:
        return client.read(
            "read.gq",
            "search_by_repo_and_kind",
            {"query": query, "repo": detected, "kind": kind},
        )
    if detected:
        return client.read(
            "read.gq", "search_by_repo", {"query": query, "repo": detected}
        )
    if kind:
        return client.read("read.gq", "search_by_kind", {"query": query, "kind": kind})
    return client.read("read.gq", "search_all", {"query": query})


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


@mcp.tool
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
        Free-text search query. Searched against ``content``.
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
    return _rerank(
        rows,
        now=datetime.now(timezone.utc),
        rank_cfg=rank_cfg,
        edge_index=edge_index,
    )


@mcp.tool
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
) -> dict:
    """Create a Memory node — shared by memory_store and workflow_trace_mine."""
    if isinstance(tags, str):
        tags = [tags]
    if isinstance(symbol_refs, str):
        symbol_refs = [symbol_refs]
    now = _now_iso()
    slug = _make_slug(kind, title)
    detected_repo = repo_module.detect(override=repo)

    client.change(
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
            "author": cfg.author,
            "tags": tags,
            "symbol_refs": symbol_refs,
            "confidence": confidence,
            "created_at": now,
            "updated_at": now,
        },
    )

    # Dual-write tags → Topic{kind:"topic"} + Tagged edge. The string list stays
    # the source of truth for old readers; the Topic graph is the new traversal
    # surface. Idempotent on the topic slug, so shared tags reuse one node. Skip
    # blank tags and dedup so neither drives redundant upsert/link calls.
    for tag in dict.fromkeys(t for t in (tags or []) if t.strip()):
        _tag_memory(slug, tag, "topic")

    # Provenance: record which session produced this memory (best-effort). The
    # engine validates edge endpoints, so a stale /tmp state file pointing at a
    # session that no longer exists in the store would raise — swallow it so a
    # provenance failure never blocks the memory write.
    active = _active_session_slug()
    session_linked = False
    if active:
        try:
            client.change(
                "mutations.gq",
                "link_session_produced",
                {"from": active, "to": slug},
            )
            _invalidate_edge_index()  # SessionProduced feeds corroboration
            session_linked = True
        except RuntimeError:
            pass

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
            "first if this memory should roll up to the project's history."
        )
    return result


@mcp.tool
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
    """
    # When no repo is known (not passed, and detection finds none), offer to
    # scope it rather than silently persisting an unscoped node. Falls back to
    # None (today's behavior) under automation / an unsupported client.
    repo = await elicit.repo_or_detect(ctx, repo)
    return _store_memory(
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
    )


@mcp.tool
def memory_get(slug: str, include_topics: bool = False) -> dict | None:
    """
    Retrieve a single memory by its slug.

    Returns the full node or ``null`` if not found.

    Parameters
    ----------
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


@mcp.tool
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
        if not client.read("read.gq", "get_memory", {"slug": from_slug}):
            return {
                "from": from_slug,
                "to": to_slug,
                "kind": kind,
                "linked": False,
                "missing": [from_slug],
            }
        topic_slug = _resolve_topic(to_slug)
        if topic_slug is None:
            return {
                "from": from_slug,
                "to": to_slug,
                "kind": kind,
                "linked": False,
                "missing": [to_slug],
            }
        client.change(
            "mutations.gq", "link_tagged", {"from": from_slug, "to": topic_slug}
        )
        return {"from": from_slug, "to": topic_slug, "kind": kind, "linked": True}

    endpoints = {from_slug, to_slug}
    present = {
        slug
        for slug in endpoints
        if client.read("read.gq", "get_memory", {"slug": slug})
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
    ):
        return {
            "from": from_slug,
            "to": to_slug,
            "kind": kind,
            "linked": False,
            "reason": "declined",
        }

    client.change(
        "mutations.gq",
        _MEMORY_LINK_MUTATIONS[kind],
        {"from": from_slug, "to": to_slug},
    )
    _invalidate_edge_index()  # supersede/contradict/support sets changed
    return {"from": from_slug, "to": to_slug, "kind": kind, "linked": True}


@mcp.tool
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


@mcp.tool
def topic_get(topic: str) -> dict | None:
    """
    Resolve a Topic and return it with the memories tagged to it.

    ``topic`` is either a Topic slug (``tp-...``) or a ``name:kind`` spec
    (e.g. ``uv:topic``). Because topics are a cross-repo join surface, the
    returned memories may span repositories — this is the traversal-based
    retrieval primitive: two memories in different repos sharing a topic are
    one hop apart.

    Returns ``{"topic": {...}, "memories": [...]}`` or ``null`` if no such Topic.
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


def _upsert_code_branch(repo: str, branch: str) -> str:
    """Return the CodeBranch slug for (repo, branch), creating/touching it.

    A fresh branch is inserted with status ``active``; an existing one is
    just touched (``updated_at`` bumped, status reset to ``active`` — a
    branch resuming work after being marked ``abandoned`` is active again).
    """
    slug = _code_branch_slug(repo, branch)
    now = _now_iso()
    if client.read("read.gq", "get_code_branch", {"slug": slug}):
        client.change(
            "mutations.gq",
            "touch_code_branch",
            {"slug": slug, "status": "active", "updated_at": now},
        )
    else:
        client.change(
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
    return slug


def _link_works_on(branch_slug: str, task_slug: str) -> None:
    """Idempotent WorksOn edge — task_claim re-calls (lease renewal) must not
    pile up duplicate edges between the same branch and task."""
    if not client.read(
        "read.gq",
        "code_branch_works_on_edge",
        {"branch_slug": branch_slug, "task_slug": task_slug},
    ):
        client.change(
            "mutations.gq", "link_works_on", {"from": branch_slug, "to": task_slug}
        )


def _link_for_project(branch_slug: str, project_slug: str) -> None:
    """Idempotent ForProject edge — see ``_link_works_on``."""
    if not client.read(
        "read.gq",
        "code_branch_for_project_edge",
        {"branch_slug": branch_slug, "project_slug": project_slug},
    ):
        client.change(
            "mutations.gq",
            "link_for_project",
            {"from": branch_slug, "to": project_slug},
        )


def _track_code_branch(
    repo: str | None, *, task_slug: str | None = None, project_slug: str | None = None
) -> None:
    """Best-effort CodeBranch upsert + edge link for the current checkout.

    No-ops when there's no repo context or no current git branch (detached
    HEAD, outside a repo) — never raises, since this is metadata riding
    alongside a task/workflow tool call, not the tool's actual purpose.
    """
    if not repo:
        return
    branch = repo_module.current_branch()
    if not branch:
        return
    try:
        slug = _upsert_code_branch(repo, branch)
        if task_slug:
            _link_works_on(slug, task_slug)
        if project_slug:
            _link_for_project(slug, project_slug)
    except Exception as exc:  # noqa: BLE001 — coordination metadata, never fatal
        print(f"witan: CodeBranch tracking failed: {exc}", file=sys.stderr)


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


# Session-state path lives in ``session_state`` (shared with the Stop hook so
# the writer and reader can't diverge under a custom TMPDIR).
_STATE_FILE_PREFIX = session_state.STATE_FILE_PREFIX
_session_state_path = session_state.session_state_path


def _active_session_slug() -> str | None:
    """The WorkflowSession slug for the current agent session, or None.

    Reads the state file ``workflow_session_start`` wrote, keyed by
    ``$CLAUDE_SESSION_ID``. Fails soft on any missing-env/read/parse error —
    provenance is best-effort and must never block a memory write.
    """
    session_id = os.environ.get("CLAUDE_SESSION_ID")
    # Validate before building a path with it: reject anything that isn't a plain
    # session id so a crafted value can't redirect the read outside the temp dir.
    if not session_id or not re.fullmatch(r"[A-Za-z0-9_.-]+", session_id):
        return None
    try:
        state = json.loads(_session_state_path(session_id).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    # A corrupt file can be valid JSON but not an object (e.g. `[]`/`null`);
    # guard so .get() can't raise AttributeError and break the write.
    if not isinstance(state, dict):
        return None
    return state.get("session_slug") or None


@mcp.tool
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
        repo-less "floating" project from outside any git repo.
    github_issue:
        URL of the GitHub issue tracking this work.
        e.g. ``github.com/mitodl/ol-django/issues/847``.
    tags:
        Optional list of tags for grouping and searching.
    """
    now = _now_iso()
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
            "author": cfg.author,
            "tags": tags,
            "github_issue": github_issue,
            "github_pr": None,
            "created_at": now,
            "updated_at": now,
        },
    )
    return {"slug": slug, "repos": repo_set, "phase": phase}


@mcp.tool
def workflow_project_get(slug: str) -> dict | None:
    """
    Retrieve a single workflow project by slug.

    Returns the full project node (including ``blocked_by`` and ``blocks``
    lists) or ``null`` if not found.

    ``blocked_by`` lists the ``wp-`` slugs of projects that must complete
    before this one is ready. ``blocks`` lists projects this project is
    currently blocking (derived by scanning all active projects).
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


def _latest_session_summary(project_slug: str) -> dict | None:
    """The most-recently-started session for a project, condensed for resume.

    ``list_sessions_by_project`` returns sessions ordered by ``started_at`` asc,
    so the last row is the latest. Returns ``None`` when the project has no
    sessions yet. Shared by ``workflow_project_status`` and the context hook so
    "where things stand" reads the same everywhere.
    """
    sessions = client.read(
        "read.gq", "list_sessions_by_project", {"project_slug": project_slug}
    )
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


@mcp.tool
def workflow_project_status(slug: str) -> dict | None:
    """One-call "what should I do next" resume view for a workflow project.

    Combines the four things an agent (or the context hook) needs to pick up a
    project without re-deriving state: current **phase**, the **ready tasks**
    under it (same readiness rule as ``task_ready``), the **last session's
    handoff summary** (and whether it's still open), and any project-level
    **blockers**. Returns ``None`` if the project doesn't exist.
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


@mcp.tool
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


@mcp.tool
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
    now = _now_iso()
    before = client.read("read.gq", "get_workflow_project", {"slug": slug})
    prev_phase = before[0].get("phase") if before else None
    advisory = _advance_advisory(prev_phase, phase)

    # A backward/skip transition (advisory set AND the phase actually changes) is
    # confirmed before committing. Headless/unsupported clients proceed as before;
    # only an explicit decline aborts, leaving the project untouched.
    if (
        advisory
        and prev_phase != phase
        and not await elicit.confirm(
            ctx, f"{advisory} Proceed with the advance?", default_when_unsupported=True
        )
    ):
        current = before[0] if before else {"slug": slug, "phase": prev_phase}
        return {**current, "advisory": advisory, "advanced": False}

    client.change(
        "mutations.gq",
        "update_workflow_project_phase",
        {"slug": slug, "phase": phase, "github_pr": github_pr, "updated_at": now},
    )
    rows = client.read("read.gq", "get_workflow_project", {"slug": slug})
    result = rows[0] if rows else {"slug": slug, "phase": phase}

    # Soft validation: flag an unusual transition without blocking it.
    if advisory:
        result["advisory"] = advisory
    return result


@mcp.tool
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
    now = _now_iso()

    trace_slug = f"wt-{slug}"
    existing = client.read("read.gq", "get_trace", {"slug": trace_slug})
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
        )

    client.change(
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

    project_rows = client.read("read.gq", "get_workflow_project", {"slug": slug})
    project = project_rows[0] if project_rows else {}

    sessions = client.read(
        "read.gq", "list_sessions_by_project", {"project_slug": slug}
    )

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

    client.change(
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
            "author": cfg.author,
            "tags": project.get("tags"),
            "created_at": now,
        },
    )
    client.change(
        "mutations.gq",
        "link_produced",
        {"from": slug, "to": trace_slug},
    )

    return {"project_slug": slug, "trace_slug": trace_slug, "existed": False}


@mcp.tool
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


@mcp.tool
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

    Returns ``{"project_slug": ..., "memories": [...], "by_session": {...}}``
    (``by_session`` is empty unless ``group_by_session`` is set).
    """
    merged: dict[str, dict] = {}
    for query in ("project_produced_memories", "informed_memories"):
        for row in client.read("read.gq", query, {"project_slug": project_slug}):
            merged[row["slug"]] = row

    by_session: dict[str, list[dict]] = {}
    if group_by_session:
        sessions = client.read(
            "read.gq", "list_sessions_by_project", {"project_slug": project_slug}
        )
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


@mcp.tool
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


def _annotate_trace(
    trace_slug: str,
    lessons_slug: list[str] | None = None,
    patterns_slug: list[str] | None = None,
) -> dict:
    """Union new lesson/pattern slugs into a trace's annotation fields."""
    if isinstance(lessons_slug, str):
        lessons_slug = [lessons_slug]
    if isinstance(patterns_slug, str):
        patterns_slug = [patterns_slug]
    rows = client.read("read.gq", "get_trace", {"slug": trace_slug})
    if not rows:
        return {"slug": trace_slug, "error": "no such trace"}
    trace = rows[0]

    merged_lessons = list(
        dict.fromkeys([*(trace.get("lessons_slug") or []), *(lessons_slug or [])])
    )
    merged_patterns = list(
        dict.fromkeys([*(trace.get("patterns_slug") or []), *(patterns_slug or [])])
    )

    client.change(
        "mutations.gq",
        "update_workflow_trace_annotations",
        {
            "slug": trace_slug,
            "lessons_slug": merged_lessons or None,
            "patterns_slug": merged_patterns or None,
        },
    )
    return {
        "slug": trace_slug,
        "lessons_slug": merged_lessons or None,
        "patterns_slug": merged_patterns or None,
    }


@mcp.tool
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


@mcp.tool
def workflow_trace_mine(
    trace_slug: str,
    patterns: list[dict] | None = None,
    lessons: list[dict] | None = None,
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
        sessions = client.read(
            "read.gq",
            "list_sessions_by_project",
            {"project_slug": trace["project_slug"]},
        )
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
        )["slug"]
        for spec in lessons or []
    ]

    for slug in (*created_patterns, *created_lessons):
        client.change(
            "mutations.gq",
            "link_informed",
            {"from": trace["project_slug"], "to": slug},
        )
    if created_patterns or created_lessons:
        _invalidate_edge_index()  # Informed feeds corroboration

    _annotate_trace(
        trace_slug,
        lessons_slug=created_lessons or None,
        patterns_slug=created_patterns or None,
    )

    return {"created_patterns": created_patterns, "created_lessons": created_lessons}


@mcp.tool
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
    now = _now_iso()
    client.change(
        "mutations.gq", "link_project_blocks", {"from": slug, "to": blocks_slug}
    )
    blocked = client.read("read.gq", "get_workflow_project", {"slug": blocks_slug})
    if blocked:
        existing = blocked[0].get("blocked_by") or []
        if slug not in existing:
            client.change(
                "mutations.gq",
                "update_workflow_project_blocked_by",
                {
                    "slug": blocks_slug,
                    "blocked_by": [*existing, slug],
                    "updated_at": now,
                },
            )
    return {"blocker": slug, "blocked": blocks_slug, "linked": True}


@mcp.tool
def workflow_project_unblock(slug: str, blocks_slug: str) -> dict:
    """
    Remove a project dependency declared with ``workflow_project_block``.

    Removes ``slug`` from ``blocks_slug.blocked_by``. The ``ProjectBlocks``
    graph edge is not deleted (omnigraph edges are append-only), but the
    denormalized field — which drives the ready-work check — is updated.

    Parameters
    ----------
    slug:
        The ``wp-`` slug of the *blocking* project to remove.
    blocks_slug:
        The ``wp-`` slug of the project to unblock.
    """
    now = _now_iso()
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


@mcp.tool
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


@mcp.tool
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

    Also writes a state file to ``/tmp`` so the ``Stop`` hook can close the
    session automatically if ``workflow_session_end`` is not called explicitly.

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
    now = _now_iso()
    slug = _make_slug("workflow_session", project_slug)
    detected_repo = repo_module.detect(override=repo)

    client.change(
        "mutations.gq",
        "insert_workflow_session",
        {
            "slug": slug,
            "project_slug": project_slug,
            "session_id": session_id,
            "repo": detected_repo,
            "phase": phase,
            "summary": "",
            "author": cfg.author,
            "tags": tags,
            "started_at": now,
        },
    )
    client.change(
        "mutations.gq",
        "link_belongs_to",
        {"from": slug, "to": project_slug},
    )

    # Accrete this session's repo into the project's repo set, so a project's
    # association grows as it's worked across repos without explicit declaration.
    if detected_repo:
        project_rows = client.read(
            "read.gq", "get_workflow_project", {"slug": project_slug}
        )
        if project_rows:
            current = _project_repos(project_rows[0])
            if detected_repo not in current:
                client.change(
                    "mutations.gq",
                    "update_workflow_project_repos",
                    {
                        "slug": project_slug,
                        "repos": _merge_repos(current, detected_repo),
                        "updated_at": now,
                    },
                )

    _track_code_branch(detected_repo, project_slug=project_slug)

    # Write state file so Stop hook can close this session
    state = {"session_slug": slug, "project_slug": project_slug, "started_at": now}
    state_path = _session_state_path(session_id)
    try:
        state_path.write_text(json.dumps(state))
    except OSError:
        pass

    return {"session_slug": slug, "project_slug": project_slug, "phase": phase}


@mcp.tool
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
    now = _now_iso()
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

    # Clean up state file for any session_id that maps to this slug
    # (best-effort; Stop hook will also attempt cleanup)
    for state_file in session_state.iter_session_state_files():
        try:
            data = json.loads(state_file.read_text())
            if data.get("session_slug") == session_slug:
                state_file.unlink(missing_ok=True)
                break
        except (OSError, json.JSONDecodeError):
            continue

    return {"session_slug": session_slug, "ended_at": now}


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
    slug: str, changes: dict, *, surface_conflict: bool = False
) -> dict | None:
    """Read a task, merge ``changes`` over its mutable fields, write it back.

    Mirrors the read-merge-write pattern documented for ``update_memory`` so we
    avoid per-field update queries. Returns the updated node or ``None``.

    ``surface_conflict`` propagates to the write so a compare-and-swap caller
    (``task_claim``) sees an :class:`~witan.graph.OmnigraphConflict` on a lost
    optimistic-concurrency race instead of the write being silently retried.
    """
    rows = client.read("read.gq", "get_task", {"slug": slug})
    if not rows:
        return None
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
        "updated_at": _now_iso(),
    }
    client.change(
        "mutations.gq", "update_task", merged, surface_conflict=surface_conflict
    )
    return client.read("read.gq", "get_task", {"slug": slug})[0]


@mcp.tool
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
    now = _now_iso()
    slug = _make_slug("task", title)
    # Offer to scope the task when nothing is detected; falls back to an
    # unscoped task (today's behavior) under automation / an unsupported client.
    repo = await elicit.repo_or_detect(ctx, repo)
    detected_repo = repo_module.detect(override=repo)
    # Only "blocked" if a blocker is not already closed — otherwise it's ready
    # now and would never be auto-unblocked.
    status: TaskStatus = "open"
    for blocker_slug in blocked_by or []:
        fetched = client.read("read.gq", "get_task", {"slug": blocker_slug})
        if fetched and fetched[0].get("status") != "closed":
            status = "blocked"
            break

    client.change(
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
            "author": cfg.author,
            "symbol_refs": symbol_refs,
            "tags": tags,
            "created_at": now,
            "updated_at": now,
            "claimed_at": None,
        },
    )

    if project_slug:
        client.change(
            "mutations.gq", "link_task_belongs_to", {"from": slug, "to": project_slug}
        )
    if parent:
        client.change("mutations.gq", "link_parent_of", {"from": parent, "to": slug})
    for blocker in blocked_by or []:
        client.change("mutations.gq", "link_blocks", {"from": blocker, "to": slug})
    for source in discovered_from or []:
        client.change(
            "mutations.gq", "link_discovered_from", {"from": slug, "to": source}
        )

    return {"slug": slug, "status": status, "repo": detected_repo}


@mcp.tool
def task_get(slug: str) -> dict | None:
    """Retrieve a single task by slug. Returns the full node or ``null``."""
    rows = client.read("read.gq", "get_task", {"slug": slug})
    return rows[0] if rows else None


@mcp.tool
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
        rows = [r for r in rows if r.get("assignee") == assignee]
    return rows


@mcp.tool
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
    repo:
        Canonical repo URI to (re)assign this task to. Pass an explicit value to
        correct tasks that were created without proper repo context.
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
            changes["closed_at"] = _now_iso()

    updated = _update_task(slug, changes)

    if parent is not None and updated is not None:
        client.change("mutations.gq", "link_parent_of", {"from": parent, "to": slug})
        updated = _update_task(slug, {"parent_slug": parent})

    # Closing here must unblock dependents too, matching task_close.
    if status == "closed" and updated is not None:
        _unblock_dependents(updated.get("repo"))

    return updated


@mcp.tool
def task_close(slug: str, resolution: str | None = None) -> dict | None:
    """
    Close a task: set status ``closed``, stamp ``closed_at``, record a resolution.

    Closing a blocker is what unblocks its dependents — they become visible to
    ``task_ready`` once every blocker is closed.
    """
    closed = _update_task(
        slug,
        {"status": "closed", "closed_at": _now_iso(), "resolution": resolution},
    )
    if closed:
        _unblock_dependents(closed.get("repo"))
    return closed


@mcp.tool
async def task_claim(
    slug: str,
    assignee: str | None = None,
    force: bool = False,
    ctx: Context | None = None,
) -> dict | None:
    """
    Claim a task for work: set it ``in_progress`` under ``assignee`` with a lease.

    The coordination primitive for parallel/multi-user agents — call it before
    starting a ready task so others see it is taken. Returns ``{"claimed": true,
    …}`` on success, or ``{"claimed": false, "reason": …}`` when the task is
    closed, still blocked, or held by someone else. The lease (``claimed_at``)
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
        Holder identity. Defaults to the configured author; parallel agents under
        one identity should pass a distinct id (e.g. a session id) so claims don't
        collide.
    force:
        Steal the task even if another holder's lease is still valid.
    """
    # Best-effort CAS, not a hard lock: omnigraph has no conditional-write, so we
    # surface OCC conflicts and post-write-verify ownership rather than trusting
    # last-write-wins (see docs/adr/0003 and the claim loop below). On success
    # also upserts a CodeBranch for the checkout's repo+branch and links it
    # WorksOn this task (best-effort).
    holder = assignee or cfg.author
    rows = client.read("read.gq", "get_task", {"slug": slug})
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
        _unblock_dependents(task.get("repo"))
        rows = client.read("read.gq", "get_task", {"slug": slug})
        if not rows or rows[0].get("status") == "blocked":
            return {"slug": slug, "claimed": False, "reason": "blocked"}
        task = rows[0]
        status = task.get("status")

    current_holder = task.get("assignee")
    claimed_at = task.get("claimed_at")
    held = status == "in_progress" and current_holder and not _lease_expired(claimed_at)
    if held and current_holder != holder and not force:
        # Offer to steal instead of a flat refusal. Headless/unsupported clients
        # get the historical behavior (no steal); an explicit confirm proceeds as
        # if ``force`` had been passed.
        stole = await elicit.confirm(
            ctx,
            f"Task {slug} is held by {current_holder} (claimed {claimed_at}). "
            "Steal the claim?",
            default_when_unsupported=False,
        )
        if not stole:
            return {
                "slug": slug,
                "claimed": False,
                "reason": "held",
                "held_by": current_holder,
                "claimed_at": claimed_at,
            }
        force = True

    now = _now_iso()
    claim = {"status": "in_progress", "assignee": holder, "claimed_at": now}
    # omnigraph has no conditional-write (CAS) primitive, so on each surfaced
    # optimistic-concurrency conflict we re-read and either bail (a rival won) or
    # re-attempt the claim. surface_conflict stays on for every attempt — a
    # consecutive conflict must never fall back to the blind-retry path, which
    # would re-apply the claim over whoever committed in the meantime.
    for attempt in range(_CLAIM_MAX_ATTEMPTS):
        try:
            _update_task(slug, claim, surface_conflict=True)
            break
        except OmnigraphConflict:
            # A concurrent writer committed between our read and our write.
            rows = client.read("read.gq", "get_task", {"slug": slug})
            fresh = rows[0] if rows else {}
            rival = fresh.get("assignee")
            if (
                fresh.get("status") == "in_progress"
                and rival
                and rival != holder
                and not _lease_expired(fresh.get("claimed_at"))
                and not force
            ):
                return {
                    "slug": slug,
                    "claimed": False,
                    "reason": "lost_race",
                    "held_by": rival,
                    "claimed_at": fresh.get("claimed_at"),
                }
            # The conflict was unrelated (or the rival's lease has lapsed) —
            # retry the claim now that the manifest has advanced.
            if attempt + 1 == _CLAIM_MAX_ATTEMPTS:
                raise

    # Post-write verification: with no store-level CAS, a rival's claim could
    # still have landed last. Re-read and confirm we actually hold it before
    # reporting success, so at most one caller ever sees claimed=True.
    rows = client.read("read.gq", "get_task", {"slug": slug})
    winner = rows[0].get("assignee") if rows else None
    if winner != holder and not force:
        return {
            "slug": slug,
            "claimed": False,
            "reason": "lost_race",
            "held_by": winner,
            "claimed_at": rows[0].get("claimed_at") if rows else None,
        }
    _track_code_branch(repo_module.detect(), task_slug=slug)
    return {
        "slug": slug,
        "claimed": True,
        "assignee": holder,
        "claimed_at": now,
        "stole": bool(held and current_holder != holder),
    }


@mcp.tool
def task_release(
    slug: str,
    assignee: str | None = None,
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
        Holder identity releasing the task. Defaults to the configured author.
    status:
        Status to return the task to (default ``open``).
    force:
        Release even if held by a different assignee.
    """
    holder = assignee or cfg.author
    rows = client.read("read.gq", "get_task", {"slug": slug})
    if not rows:
        return None
    current_holder = rows[0].get("assignee")
    if current_holder and current_holder != holder and not force:
        return {"slug": slug, "released": False, "held_by": current_holder}

    _update_task(slug, {"status": status, "assignee": None, "claimed_at": None})
    return {"slug": slug, "released": True, "status": status}


@mcp.tool
def task_ready(
    repo: str | None = None,
    project_slug: str | None = None,
    assignee: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """
    Return ready-to-work tasks: not-yet-started tasks whose blockers are all closed.

    A task is ready when its status is ``open`` or ``blocked`` (i.e. nobody is on it
    yet and it is not closed) AND every task in its ``blocked_by`` list is closed.
    This is the core coordination primitive — call it to pick the next actionable
    item without manual triage. Results are ordered by priority (``p0`` first).

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
        and (assignee is None or r.get("assignee") == assignee)
    ]
    ready.sort(key=lambda r: _PRIORITY_ORDER.get(r.get("priority"), 9))
    return ready[:limit]


@mcp.tool
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
    """
    if kind == "blocks":
        client.change("mutations.gq", "link_blocks", {"from": from_slug, "to": to_slug})
        blocked = client.read("read.gq", "get_task", {"slug": to_slug})
        if blocked:
            existing = blocked[0].get("blocked_by") or []
            if from_slug not in existing:
                changes = {"blocked_by": [*existing, from_slug]}
                if blocked[0].get("status") == "open":
                    blocker = client.read("read.gq", "get_task", {"slug": from_slug})
                    if blocker and blocker[0].get("status") != "closed":
                        changes["status"] = "blocked"
                _update_task(to_slug, changes)
    elif kind == "parent":
        client.change(
            "mutations.gq", "link_parent_of", {"from": from_slug, "to": to_slug}
        )
        _update_task(to_slug, {"parent_slug": from_slug})
    elif kind == "discovered_from":
        client.change(
            "mutations.gq", "link_discovered_from", {"from": from_slug, "to": to_slug}
        )
    elif kind == "addresses":
        client.change(
            "mutations.gq", "link_addresses", {"from": from_slug, "to": to_slug}
        )

    return {"from": from_slug, "to": to_slug, "kind": kind}


@mcp.tool
def symbol_context(symbol_id: str) -> dict:
    """
    Memories and tasks attached to a code symbol (direction: symbol → work).

    The reverse of ``memory_symbols``: given a symbol id, returns the memories and
    tasks whose ``symbol_refs`` include it — "what lessons and open tasks concern
    this function?". Call it after locating a symbol with the witan-code ``code_*``
    tools, before editing.

    ``symbol_id`` has the form ``<repo>#<path/to/file.py>::<QualifiedName>``; the
    repo prefix scopes the lookup, or the current repo when the id has no ``#``.
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
            _code_server._cached = None
    return _code_server._cached


@mcp.tool
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
            pass

    return {
        "key_norm": key_norm,
        "kind": kind,
        "memories": memories,
        "bindings": bindings,
    }


@mcp.tool
def memory_symbols(slug: str) -> dict:
    """
    Code symbols a memory concerns (direction: memory → symbols).

    The reverse of ``symbol_context``: returns each of the memory's ``symbol_refs``,
    enriched with the live definition when witan-code is reachable, or the raw ref
    strings otherwise.
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
                pass
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


@mcp.tool
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
