"""Write path for the shared cross-repo bridge store (_bridge.omni).

Runs as a SEPARATE phase after the per-repo store write completes, so the two
stores' advisory write locks are never held at once (no nesting → no deadlock),
and a bridge failure can't corrupt a per-repo store that already succeeded.
"""

from datetime import datetime, timezone

from . import config as cfg_module
from .bridge_extractors import ParsedBinding
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

    records = _dedupe([_record(b, repo) for b in bindings])
    client.load(records, mode="merge")
    return len(records)


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
