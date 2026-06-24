"""Write path for the shared cross-repo bridge store (_bridge.omni).

Runs as a SEPARATE phase after the per-repo store write completes, so the two
stores' advisory write locks are never held at once (no nesting → no deadlock),
and a bridge failure can't corrupt a per-repo store that already succeeded.
"""

from datetime import datetime, timezone

from . import config as cfg_module
from .bridge_extractors import ParsedBinding, adjust_confidence
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
) -> int:
    """Purge stale bindings and merge in fresh ones for ``repo``.

    full_repo: delete every binding for the repo first (also clears bindings for
    files deleted from disk), then reload. Incremental: delete only the touched
    source files' bindings so sibling files' bindings survive.

    Store-level confidence adjustments (self_provided_key, known_provider_package)
    are applied here before writing, using provider data already present in the
    bridge store from previously-indexed repos.

    Returns the number of binding records written.
    """
    # Skip creating the store for a contract-less repo on its first index.
    if not bindings and not bridge_store(cfg).exists():
        return 0

    store = ensure_bridge_store(cfg)
    client = OmnigraphClient(str(store), cfg.queries_dir)

    if full_repo:
        client.change("bridge.gq", "delete_bindings_for_repo", {"repo": repo})
    else:
        for rel in touched_files:
            client.change(
                "bridge.gq", "delete_bindings_in_file", {"repo_file": f"{repo}|{rel}"}
            )

    # Collect store-level context for confidence adjustments.
    # provider_keys: (repo, key_norm) for ALL provider records in the store
    # (including the consumer repo's own rows) plus provider bindings from the
    # current batch.  A full-repo reindex deletes the repo's rows before this
    # query, so the current bindings must be included explicitly to allow the
    # self_provided_key penalty to fire when the same repo both provides and
    # consumes a key_norm.
    # provider_pkg_slugs: key_norm values for package providers from OTHER repos.
    try:
        all_rows = client.read("bridge.gq", "all_bindings", {})
        provider_keys: frozenset[tuple[str, str]] = frozenset(
            (r["repo"], r["key_norm"]) for r in all_rows if r.get("role") == "provider"
        ) | frozenset((repo, b.key_norm) for b in bindings if b.role == "provider")
        provider_pkg_slugs: frozenset[str] = frozenset(
            r["key_norm"]
            for r in all_rows
            if r.get("role") == "provider"
            and r.get("kind") == "package"
            and r.get("repo") != repo
        )
    except Exception:  # noqa: BLE001 — store may be empty or query unavailable
        provider_keys = frozenset(
            (repo, b.key_norm) for b in bindings if b.role == "provider"
        )
        provider_pkg_slugs = frozenset()

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

    records = _dedupe([_record(b, repo) for b in adjusted])
    client.load(records, mode="merge")
    return len(records)


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
