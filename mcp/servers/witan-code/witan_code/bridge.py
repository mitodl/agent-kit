"""Write path for the shared cross-repo bridge store (_bridge.omni).

Runs as a SEPARATE phase after the per-repo store write completes, so the two
stores' advisory write locks are never held at once (no nesting → no deadlock),
and a bridge failure can't corrupt a per-repo store that already succeeded.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from witan_core import now_iso
from witan_core.observability import get_logger

from . import config as cfg_module
from . import package_map, views
from .bridge_extractors import (
    ParsedBinding,
    adjust_confidence,
    canonical_symbol,
    consumed_package,
    parse_symbol,
)
from .graph import OmnigraphClient, check_writable
from .store import bridge_store, ensure_bridge_store

# Delete statements per commit when purging a repo's bindings. A full-repo purge
# has to be capped rather than built whole.
#
# Same reasoning, and the same measurement, as `_DELETE_BATCH_SIZE` in
# `indexer.py` — read the comment there. Short version: the argv justification is
# stale (the deployed path is the HTTP transport, no argv), a matching delete
# costs ~362ms, and 500 of them is ~181s against a 120s client timeout. One
# statement per file here rather than two, so this is 128 files per commit.
#
# Lowered together with the indexer's cap deliberately. This purge has not been
# SEEN to time out, but it is the identical shape against the same engine, and a
# repo with enough changed files would trip it the same way. Fixing only the one
# that happened to fail first would leave the other waiting for a busy day.
# (The 362ms figure was measured on `Symbol` deletes; `InterfaceBinding` deletes
# were not timed separately, so treat this cap as the same order, not as exact.)
_PURGE_BATCH_SIZE = 128


logger = get_logger("witan.code.bridge")


@dataclass(frozen=True)
class ExternalUse:
    """One use of a name imported from another repo's package namespace: an
    ``ExternalUse`` row (bridge-schema.pg). ``line`` is 1-based."""

    file: str
    line: int
    symbol_id: str | None
    target: str
    language: str


def write_bindings(
    bindings: list[ParsedBinding],
    repo: str,
    cfg: cfg_module.Config,
    *,
    external_uses: Sequence[ExternalUse] = (),
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

    ``external_uses`` are the parsed files' uses of other repos' names. A
    file's stored uses are replaced whenever its bindings would be: those in
    the purge set, which covers a file that left the repo even when it never
    carried a binding (``from mitol import common`` matches no package
    consumer), because the files are read from the stored uses themselves.

    Returns the number of binding records written.
    """
    # Skip creating the store for a contract-less repo on its first index.
    if not bindings and not external_uses and not bridge_store(cfg).exists(cfg):
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
    except Exception:  # store may be empty or query unavailable
        # Info: an empty bridge on a fresh store is normal, but this read also
        # silently disables dedup against existing bindings, so it is worth a
        # line when a populated bridge starts answering nothing.
        logger.info("witan.code.bridge.all_bindings_failed", exc_info=True)
        all_rows = []

    # provider_keys/provider_pkg_slugs only feed adjust_confidence, which only
    # runs for endpoint consumer bindings — skip the extra full-store
    # RepoSymbol read entirely when this batch has none to adjust.
    has_endpoint_consumers = any(
        b.kind == "endpoint" and b.role == "consumer" for b in bindings
    )
    repo_symbol_rows = _read_repo_symbols(client) if has_endpoint_consumers else []

    use_files = _stored_use_files(client, repo)
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
        purge_files |= {rel for rel in use_files or () if _gone(rel)}
    # Only the files that hold uses get a delete for them: a delete costs about
    # as much as a binding delete, and most files hold none.
    use_purge = purge_files if use_files is None else purge_files & use_files
    # One commit for the whole purge, not one per file: this runs over every
    # touched file on each index, and a commit apiece fragments the bridge store
    # the same way the per-file deletes did the per-repo one. Chunked because a
    # full-repo purge is unbounded and a matching delete costs ~362ms, so an
    # uncapped batch runs past the client's 120s timeout — see
    # `_PURGE_BATCH_SIZE` for the measurements and for why the argv reasoning
    # this comment used to give no longer applies.
    #
    # The repo's symbol table is cleared in the same batch, because it is
    # rebuilt below from `all_rows` and the fresh bindings, both already read.
    # Nothing read between here and the load depends on this repo's
    # RepoSymbol rows (`repo_symbol_rows` was read above, and the package-map
    # read excludes this repo). As its own commit it cost one more slot in the
    # bridge's serialized commit lane on every index.
    client.change_many(
        [
            *(
                ("bridge.gq", "delete_bindings_in_file", {"repo": repo, "file": rel})
                for rel in sorted(purge_files)
            ),
            *(
                (
                    "bridge.gq",
                    "delete_external_uses_in_file",
                    {"repo": repo, "file": rel},
                )
                for rel in sorted(use_purge)
            ),
            ("bridge.gq", "delete_repo_symbols", {"repo": repo}),
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

    records = _dedupe([_record(b, repo) for b in adjusted])
    n_bindings = len(records)
    records.extend(_symbol_table_records(repo, surviving, adjusted))
    if full_repo:
        records.append(_package_map_record(identity, repo))
    # Deduped apart from the bindings: two uses of one name on one line share
    # a slug, and a load carrying a slug twice is refused whole.
    records.extend(_dedupe([_external_use_record(u, repo) for u in external_uses]))
    client.load(records, mode="merge")
    return n_bindings


def _stored_use_files(client: OmnigraphClient, repo: str) -> set[str] | None:
    """The files ``repo``'s stored uses are in, or ``None`` when the read fails.

    Through a deployed witan the read runs that server's copy of the query,
    which one older than this release lacks. Failing the bridge write over it
    would freeze the branch's bindings too, so the caller instead deletes uses
    in every file it purges and cannot find the uses of a file that left the
    repo with no binding, until the server is upgraded.
    """
    try:
        rows = client.read("bridge.gq", "external_use_files", {"repo": repo})
    except Exception:  # a deployment older than the query
        # Warning: the write goes on, but a deleted file's uses outlive it.
        logger.warning("witan.code.bridge.external_use_files_failed", exc_info=True)
        return None
    return {r["file"] for r in rows}


def _external_use_record(use: ExternalUse, repo: str) -> dict:
    return {
        "type": "ExternalUse",
        "data": {
            "slug": f"{repo}|{use.file}|{use.line}|{use.target}",
            "repo": repo,
            "file": use.file,
            "line": use.line,
            "symbol_id": use.symbol_id,
            "target": use.target,
            "language": use.language,
            "indexed_at": now_iso(),
        },
    }


class PackageAlias(NamedTuple):
    """The name a package's re-export gives a definition in its submodule.

    ``name`` is valid only when one of ``init_files`` (the package's
    ``__init__``, by CodeFile id) has an Imports edge to ``exported``, the
    definition's top-level symbol: the indexer writes one for each name the
    package re-exports from that submodule (``indexer._reexport_edges``).
    """

    init_files: tuple[str, ...]
    exported: str
    name: str


def imported_as(symbol_id: str) -> list[str]:
    """The dotted name another repo imports the definition ``symbol_id`` by
    through its own module, for matching ``ExternalUse.target``: the module
    path from the namespace root. Empty outside the shared package
    namespaces. A module symbol (``::<module>``) is the module's own name.

    :func:`package_alias` gives the package's name for it, when it has one.
    """
    found = _module_path(symbol_id)
    if found is None:
        return []
    module, _, qualified_name = found
    if qualified_name == "<module>":
        return [".".join(module)]
    return [".".join((*module, qualified_name))]


def package_alias(symbol_id: str) -> PackageAlias | None:
    """The name the definition ``symbol_id`` has if its package re-exports it,
    and what says whether it does.

    ol-django's packages re-export some submodules
    (``mitol/common/utils/__init__.py`` star-imports ``datetime``, so
    ``now_in_utc`` is also ``mitol.common.utils.now_in_utc``) but not every
    one (``helpers`` is not star-imported, so its ``max_or_none`` is not
    ``mitol.common.utils.max_or_none``, which is ``collections``'). Only a
    definition in a submodule file (not a package's own ``__init__``) under a
    package inside the namespace has one. A method's is its class's, since
    re-exporting the class re-exports the method with it.
    """
    found = _module_path(symbol_id)
    if found is None:
        return None
    module, path, qualified_name = found
    package = module[:-1]
    if (
        qualified_name == "<module>"
        or path.rpartition("/")[2].startswith("__init__.")
        or not consumed_package(".".join(package))
    ):
        return None
    repo = symbol_id.partition("#")[0]
    directory = path.rpartition("/")[0]
    top = qualified_name.partition(".")[0]
    return PackageAlias(
        init_files=tuple(
            f"{repo}#{directory}/__init__.{suffix}" for suffix in ("py", "pyi")
        ),
        exported=f"{repo}#{path}::{top}",
        name=".".join((*package, qualified_name)),
    )


def _module_path(symbol_id: str) -> tuple[list[str], str, str] | None:
    """``(dotted module parts, file path, qualified name)`` of a Python
    definition under the shared package namespaces, else ``None``."""
    _, _, rest = symbol_id.partition("#")
    path, _, qualified_name = rest.partition("::")
    stem, dot, suffix = path.rpartition(".")
    if not dot or suffix not in ("py", "pyi") or not qualified_name:
        return None
    parts = stem.split("/")
    if parts[-1] == "__init__":
        parts.pop()
    module = next(
        (
            parts[start:]
            for start in range(len(parts))
            if consumed_package(".".join(parts[start:]))
        ),
        None,
    )
    if module is None:
        return None
    return module, path, qualified_name


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
    except Exception:  # store may predate RepoSymbol
        # Debug: a store written before RepoSymbol existed legitimately has no
        # such node, and the caller only loses confidence adjustment.
        logger.debug("witan.code.bridge.repo_symbols_unavailable", exc_info=True)
        return []


def _declared_provider_packages(
    client: OmnigraphClient, *, exclude_repo: str
) -> frozenset[str]:
    """Package names declared by other repos' package maps in the bridge store."""
    try:
        rows = client.read("bridge.gq", "all_package_maps", {})
    except Exception:  # store may predate the PackageMap node
        # Debug, for the same reason as RepoSymbol above.
        logger.debug("witan.code.bridge.package_maps_unavailable", exc_info=True)
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
            # Warning: `provides` is data this repo wrote, so malformed JSON
            # here means a package map was stored wrong — not a missing
            # feature, and it silently drops that repo's provided packages.
            logger.warning("witan.code.bridge.bad_provides_json", repo=row.get("repo"))
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
        if (
            b.file == file
            and b.kind == "package"
            and b.role == "consumer"
            and b.key_norm in provider_pkg_slugs
        ):
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
