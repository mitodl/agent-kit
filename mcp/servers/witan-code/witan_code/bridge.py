"""Write path for the shared cross-repo bridge store (_bridge.omni).

Runs as a SEPARATE phase after the per-repo store write completes, so the two
stores' advisory write locks are never held at once (no nesting → no deadlock),
and a bridge failure can't corrupt a per-repo store that already succeeded.
"""

import json
from pathlib import Path

from witan_core import now_iso

from . import config as cfg_module
from . import package_map
from . import views
from .bridge_extractors import (
    ParsedBinding,
    adjust_confidence,
    canonical_symbol,
    parse_symbol,
)
from .graph import OmnigraphClient, check_writable
from .store import bridge_store, ensure_bridge_store

# Delete statements per commit when purging a repo's bindings. The composed
# query is a single argv element, so a full-repo purge has to be capped.
_PURGE_BATCH_SIZE = 500


def write_bindings(
    bindings: list[ParsedBinding],
    repo: str,
    cfg: cfg_module.Config,
    *,
    full_repo: bool,
    touched_files: tuple[str, ...] = (),
    identity: package_map.PackageIdentity | None = None,
    base: Path | None = None,
    branch: str | None = None,
    actor: str | None = None,
    indexed_files: frozenset[str] | None = None,
) -> int:
    """Purge stale bindings and merge in fresh ones for ``repo``.

    Purging is always per-file: the files (re)parsed this run, the files the
    fresh batch carries bindings for (covers Tier B sources like OpenAPI
    specs, which aren't in ``touched_files``), and — on a full-repo run — the
    files that are no longer part of the repo. An incremental full-repo index
    skips unchanged files, so a repo-wide purge here would drop those files'
    bindings with nothing in the batch to restore them.

    ``indexed_files`` is the indexer's collected set (repo-relative paths) and
    is the accurate membership test for that last group: a file can still be
    on disk yet no longer belong to this repo — a linked worktree's files are
    the case that motivated it. Callers that don't have the set fall back to
    an on-disk existence check against ``base``, which catches deletions only.

    Store-level confidence adjustments (self_provided_key, known_provider_package)
    are applied here before writing. The cross-repo half of each signal is
    sourced from other repos' Stage 1 symbol tables (RepoSymbol `exported`
    rows, docs/SYMBOL_TABLE.md) — the stable, deduplicated artifact those
    repos' own writes already produced. This repo's own contribution can't
    come from the same place: its Stage 1 table hasn't been rebuilt for THIS
    write yet (that happens further down), so it's read directly from the
    surviving + fresh bindings already in hand.

    ``branch`` is the sanitized git branch (``repo_module.store_branch()``,
    ``None`` for the default git branch) and ``actor`` the identity writing
    it. When ``branch`` is set, every read/write in this call targets the
    bridge view ``[<actor>/]<sanitized-repo-slug>/<branch>``
    (:mod:`witan_code.views`, docs/BRANCH_INDEXING.md § Bridge store) instead
    of bridge ``main``: an overlay forked once from bridge main, so it starts
    as every repo's main bindings and then only ``repo``'s own writes land on
    it — the shared main view never sees in-flight branch bindings, while a
    read scoped to this branch sees this repo's in-flight state overlaid on
    everyone else's (possibly since-updated) main.

    The repo qualifier is what keeps ``feature-x`` in two repos apart on the
    one bridge graph; the actor qualifier is what keeps ``feature-x`` in two
    *checkouts* of the same repo apart. Both are needed and they compose in
    that order — actor first, so ownership stays a prefix of the name.

    Returns the number of binding records written.
    """
    # Skip creating the store for a contract-less repo on its first index.
    if not bindings and not bridge_store(cfg).exists(cfg):
        return 0

    identity = identity or package_map.fallback_identity(repo)
    store = ensure_bridge_store(cfg)
    bridge_branch = views.bridge_view(branch, repo, actor=actor) if branch else None
    client = store.client(cfg, branch=bridge_branch)
    # Bridge main is shared by every repo, and `delete_repo_symbols` below wipes
    # a repo's whole Stage-1 table — the same CI-owns-the-default-view rule as
    # the per-repo store. Checked here too rather than relying on the caller's:
    # the bridge is a distinct store with its own addressing.
    check_writable(
        is_remote=client.is_remote,
        branch=bridge_branch,
        cfg=cfg,
        slug=f"{repo} (bridge)",
        actor=actor,
    )
    # The reads below never fork; create the branch (from bridge main) first.
    client.ensure_branch()

    try:
        all_rows = client.read("bridge.gq", "all_bindings", {})
    except Exception:  # noqa: BLE001 — store may be empty or query unavailable
        all_rows = []

    # provider_keys/provider_pkg_slugs only feed adjust_confidence, which only
    # runs for endpoint consumer bindings — skip the extra full-store
    # RepoSymbol read entirely when this batch has none to adjust.
    has_endpoint_consumers = any(
        b.kind == "endpoint" and b.role == "consumer" for b in bindings
    )
    repo_symbol_rows = _read_repo_symbols(client) if has_endpoint_consumers else []

    purge_files = set(touched_files) | {b.file for b in bindings}
    if full_repo and (indexed_files is not None or base is not None):

        def _gone(rel: str) -> bool:
            if indexed_files is not None:
                return rel not in indexed_files
            return not (base / rel).exists()

        purge_files |= {
            r["file"]
            for r in all_rows
            if r.get("repo") == repo and r.get("file") and _gone(r["file"])
        }
    # One commit for the whole purge, not one per file: this runs over every
    # touched file on each index, and a commit apiece fragments the bridge store
    # the same way the per-file deletes did the per-repo one. Chunked because the
    # composed query rides in argv and a full-repo purge is unbounded.
    client.change_many(
        [
            ("bridge.gq", "delete_bindings_in_file", {"repo_file": f"{repo}|{rel}"})
            for rel in sorted(purge_files)
        ],
        chunk_size=_PURGE_BATCH_SIZE,
    )

    # Collect store-level context for confidence adjustments.
    # provider_keys: (repo, key_norm) pairs so the self_provided_key penalty
    # fires when the same repo both provides and consumes a key_norm. OTHER
    # repos' pairs come from their Stage 1 symbol table (exported rows); this
    # repo's own pairs come from its surviving (not-purged) + fresh provider
    # bindings, since its own Stage 1 table is stale until the rebuild below.
    # provider_pkg_slugs: key_norm values for package providers from OTHER
    # repos' Stage 1 symbol tables, plus package names other repos DECLARE
    # via their package map — the map is the source of truth for identities
    # a repo doesn't necessarily emit a binding for.
    provider_keys: frozenset[tuple[str, str]] = (
        frozenset(
            (r["repo"], r["key_norm"])
            for r in repo_symbol_rows
            if r.get("role") == "exported"
            and r.get("repo")
            and r.get("key_norm")
            and r.get("repo") != repo
        )
        | frozenset(
            (r["repo"], r["key_norm"])
            for r in all_rows
            if r.get("role") == "provider"
            and r.get("repo") == repo
            and r.get("key_norm")
            and r.get("file") not in purge_files
        )
        | frozenset(
            (repo, b.key_norm) for b in bindings if b.role == "provider" and b.key_norm
        )
    )
    provider_pkg_slugs: frozenset[str] = frozenset(
        r["key_norm"]
        for r in repo_symbol_rows
        if r.get("role") == "exported"
        and r.get("kind") == "package"
        and r.get("repo") != repo
        and r.get("key_norm")
    ) | _declared_provider_packages(client, exclude_repo=repo)

    # Apply store-level confidence adjustments to endpoint consumer bindings.
    adjusted: list[ParsedBinding] = []
    for b in bindings:
        if b.kind == "endpoint" and b.role == "consumer":
            # known_provider_package: True when the same source file also
            # contains a *package* consumer binding whose key_norm matches a
            # provider package slug from another repo in the bridge store.
            # Endpoint bindings don't carry package-import info themselves;
            # _file_imports_known_provider checks co-located package consumers
            # to supply this signal.
            has_known_pkg = _file_imports_known_provider(
                b.file, bindings, provider_pkg_slugs
            )
            adjust_confidence(
                b,
                consumer_repo=repo,
                provider_keys=provider_keys,
                has_known_provider_package=has_known_pkg,
            )
        adjusted.append(b)

    for b in adjusted:
        b.symbol = canonical_symbol(b, identity)

    # Rebuild the repo's symbol table (docs/SYMBOL_TABLE.md) from every binding
    # occurrence that survives this write: stored rows outside the purge set
    # plus the fresh batch. Bindings are per-occurrence; the table is the
    # deduplicated per-symbol artifact Stage 2 joins against.
    surviving = [
        r
        for r in all_rows
        if r.get("repo") == repo and r.get("file") not in purge_files
    ]
    client.change("bridge.gq", "delete_repo_symbols", {"repo": repo})

    records = _dedupe([_record(b, repo) for b in adjusted])
    n_bindings = len(records)
    records.extend(_symbol_table_records(repo, surviving, adjusted))
    if full_repo:
        records.append(_package_map_record(identity, repo))
    client.load(records, mode="merge")
    return n_bindings


# Binding role → symbol-table role. Providers export contract surface;
# consumers hold unresolved external references (RANGER import placeholders).
# The legacy "shared" role has no table semantics and is skipped.
_TABLE_ROLE = {"provider": "exported", "consumer": "external"}


def _symbol_table_records(
    repo: str,
    surviving: list[dict],
    fresh: list[ParsedBinding],
) -> list[dict]:
    """One RepoSymbol record per (role, symbol) across the given occurrences.

    Aggregates: ``n_refs`` counts occurrences, ``confidence`` keeps the max
    (absent treated as 1.0, matching readers of InterfaceBinding.confidence),
    and the exemplar file/line is the lexicographic minimum so rebuilds are
    deterministic. Rows without a symbol (pre-symbol stores) are skipped —
    they regain table coverage on the next reindex of their file.
    """
    entries = [
        (
            r.get("symbol"),
            r.get("kind"),
            r.get("role"),
            r.get("key_norm"),
            r.get("file"),
            r.get("line"),
            r.get("confidence"),
        )
        for r in surviving
    ] + [
        (b.symbol, b.kind, b.role, b.key_norm, b.file, b.line, b.confidence)
        for b in fresh
    ]

    agg: dict[tuple[str, str], dict] = {}
    for symbol, kind, role, key_norm, file, line, confidence in entries:
        table_role = _TABLE_ROLE.get(role or "")
        if not symbol or table_role is None:
            continue
        conf = 1.0 if confidence is None else float(confidence)
        loc = (file or "", line if isinstance(line, int) else 0)
        row = agg.get((table_role, symbol))
        if row is None:
            agg[(table_role, symbol)] = {
                "kind": kind,
                "key_norm": key_norm,
                "n_refs": 1,
                "confidence": conf,
                "loc": loc,
            }
        else:
            row["n_refs"] += 1
            row["confidence"] = max(row["confidence"], conf)
            row["loc"] = min(row["loc"], loc)

    now = now_iso()
    out: list[dict] = []
    for (table_role, symbol), row in agg.items():
        parsed = parse_symbol(symbol)
        if parsed is None:
            continue
        file, line = row["loc"]
        out.append(
            {
                "type": "RepoSymbol",
                "data": {
                    "slug": f"{repo}|{table_role}|{symbol}",
                    "repo": repo,
                    "role": table_role,
                    "symbol": symbol,
                    "scheme": parsed.scheme,
                    "descriptor": parsed.descriptor,
                    "key_norm": row["key_norm"],
                    "manager": parsed.manager,
                    "package": parsed.package,
                    "version": parsed.version,
                    "kind": row["kind"],
                    "n_refs": row["n_refs"],
                    "confidence": row["confidence"],
                    "file": file or None,
                    "line": line or None,
                    "indexed_at": now,
                },
            }
        )
    return out


def _read_repo_symbols(client: OmnigraphClient) -> list[dict]:
    """Every RepoSymbol row in the bridge store, best-effort.

    Empty on a store that predates Stage 1 (no RepoSymbol node yet) or any
    other query failure — the confidence heuristics that use this degrade to
    their pre-Stage-1 baseline (no cross-repo boost/penalty) rather than
    aborting the write.
    """
    try:
        return client.read("bridge.gq", "all_repo_symbols", {})
    except Exception:  # noqa: BLE001 — store may predate RepoSymbol
        return []


def _declared_provider_packages(
    client: OmnigraphClient, *, exclude_repo: str
) -> frozenset[str]:
    """Package names declared by other repos' package maps in the bridge store."""
    try:
        rows = client.read("bridge.gq", "all_package_maps", {})
    except Exception:  # noqa: BLE001 — store may predate the PackageMap node
        return frozenset()
    names: set[str] = set()
    for row in rows:
        if row.get("repo") == exclude_repo:
            continue
        if row.get("name"):
            names.add(row["name"])
        try:
            provides = json.loads(row.get("provides") or "[]")
        except (TypeError, ValueError):
            provides = []
        if not isinstance(provides, list):
            provides = []
        for entry in provides:
            if isinstance(entry, str):
                _, _, name = entry.partition(":")
                names.add(name or entry)
    return frozenset(names)


def _package_map_record(identity: package_map.PackageIdentity, repo: str) -> dict:
    return {
        "type": "PackageMap",
        "data": {
            "slug": repo,
            "repo": repo,
            "name": identity.name,
            "manager": identity.manager,
            "version": identity.version,
            "provides": json.dumps(list(identity.provides))
            if identity.provides
            else None,
            "declared": "1" if identity.declared else None,
            "indexed_at": now_iso(),
        },
    }


def _file_imports_known_provider(
    file: str,
    bindings: list[ParsedBinding],
    provider_pkg_slugs: frozenset[str],
) -> bool:
    """Return True if any package consumer binding in ``file`` matches a known provider package."""
    for b in bindings:
        if b.file == file and b.kind == "package" and b.role == "consumer":
            if b.key_norm in provider_pkg_slugs:
                return True
    return False


def _record(b: ParsedBinding, repo: str) -> dict:
    symbol_id = b.symbol_id or ""
    sub_kind = b.sub_kind or ""
    slug = f"{repo}|{b.file}|{b.kind}|{sub_kind}|{b.key_norm}|{b.role}|{symbol_id}"
    return {
        "type": "InterfaceBinding",
        "data": {
            "slug": slug,
            "kind": b.kind,
            "sub_kind": b.sub_kind or None,
            "key": b.key,
            "key_norm": b.key_norm,
            "role": b.role,
            "repo": repo,
            "file": b.file,
            "repo_file": f"{repo}|{b.file}",
            "symbol_id": b.symbol_id or None,
            "line": b.line,
            "language": b.language,
            "framework": b.framework,
            "generic": "1" if b.generic else None,
            "confidence": b.confidence,
            "symbol": b.symbol,
            "indexed_at": now_iso(),
        },
    }


def _dedupe(records: list[dict]) -> list[dict]:
    """Keep the first record per slug — a duplicate slug fails the whole load."""
    seen: set[str] = set()
    out: list[dict] = []
    for record in records:
        slug = record["data"]["slug"]
        if slug in seen:
            continue
        seen.add(slug)
        out.append(record)
    return out
