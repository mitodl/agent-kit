"""Write path for the shared cross-repo bridge store (_bridge.omni).

Runs as a SEPARATE phase after the per-repo store write completes, so the two
stores' advisory write locks are never held at once (no nesting → no deadlock),
and a bridge failure can't corrupt a per-repo store that already succeeded.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from . import config as cfg_module
from . import package_map
from .bridge_extractors import ParsedBinding, adjust_confidence, canonical_symbol
from .graph import OmnigraphClient
from .store import bridge_store, ensure_bridge_store


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_bindings(
    bindings: list[ParsedBinding],
    repo: str,
    cfg: cfg_module.Config,
    *,
    full_repo: bool,
    touched_files: tuple[str, ...] = (),
    identity: package_map.PackageIdentity | None = None,
    base: Path | None = None,
) -> int:
    """Purge stale bindings and merge in fresh ones for ``repo``.

    Purging is always per-file: the files (re)parsed this run, the files the
    fresh batch carries bindings for (covers Tier B sources like OpenAPI
    specs, which aren't in ``touched_files``), and — on a full-repo run with
    ``base`` — files whose stored bindings no longer exist on disk. An
    incremental full-repo index skips unchanged files, so a repo-wide purge
    here would drop those files' bindings with nothing in the batch to
    restore them.

    Store-level confidence adjustments (self_provided_key, known_provider_package)
    are applied here before writing, using provider data already present in the
    bridge store from previously-indexed repos.

    Returns the number of binding records written.
    """
    # Skip creating the store for a contract-less repo on its first index.
    if not bindings and not bridge_store(cfg).exists():
        return 0

    identity = identity or package_map.fallback_identity(repo)
    store = ensure_bridge_store(cfg)
    client = OmnigraphClient(str(store), cfg.queries_dir)

    try:
        all_rows = client.read("bridge.gq", "all_bindings", {})
    except Exception:  # noqa: BLE001 — store may be empty or query unavailable
        all_rows = []

    purge_files = set(touched_files) | {b.file for b in bindings}
    if full_repo and base is not None:
        purge_files |= {
            r["file"]
            for r in all_rows
            if r.get("repo") == repo
            and r.get("file")
            and not (base / r["file"]).exists()
        }
    for rel in sorted(purge_files):
        client.change(
            "bridge.gq", "delete_bindings_in_file", {"repo_file": f"{repo}|{rel}"}
        )

    # Collect store-level context for confidence adjustments.
    # provider_keys: (repo, key_norm) for provider records that will survive
    # the purge (own-repo rows in purged files are stale) plus provider
    # bindings from the current batch, so the self_provided_key penalty fires
    # when the same repo both provides and consumes a key_norm.
    # provider_pkg_slugs: key_norm values for package providers from OTHER repos,
    # plus package names other repos DECLARE via their package map — the map is
    # the source of truth; incidentally indexed package.json rows remain as a
    # fallback signal.
    provider_keys: frozenset[tuple[str, str]] = frozenset(
        (r["repo"], r["key_norm"])
        for r in all_rows
        if r.get("role") == "provider"
        and not (r.get("repo") == repo and r.get("file") in purge_files)
    ) | frozenset((repo, b.key_norm) for b in bindings if b.role == "provider")
    provider_pkg_slugs: frozenset[str] = frozenset(
        r["key_norm"]
        for r in all_rows
        if r.get("role") == "provider"
        and r.get("kind") == "package"
        and r.get("repo") != repo
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

    records = _dedupe([_record(b, repo) for b in adjusted])
    if full_repo:
        records.append(_package_map_record(identity, repo))
    client.load(records, mode="merge")
    return len(records)


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
            "indexed_at": _now_iso(),
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
            "indexed_at": _now_iso(),
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
