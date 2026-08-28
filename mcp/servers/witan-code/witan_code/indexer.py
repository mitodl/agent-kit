"""Tree-sitter code indexer for the Layer-2 code graph.

Walks a repo, extracts symbols (functions, methods, classes, modules) and
best-effort relationship edges, and writes them to a per-repo Omnigraph store.

Call/Reference/Import/Inherits resolution is HEURISTIC: identifiers are matched
to known Symbol names within the same repo, preferring same-file definitions,
then imported modules, then any repo-wide match. It is intentionally syntactic
and will miss dynamic dispatch and produce occasional false links.
"""

import contextlib
import functools
import hashlib
import importlib
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from witan_core import now_iso
from witan_core.observability import get_logger

from . import bridge as bridge_module
from . import bridge_extractors, package_map, views
from . import config as cfg_module
from . import identity as identity_module
from . import repo as repo_module
from .bridge_extractors import ParsedBinding
from .graph import OmnigraphClient, check_writable, owns_view
from .store import ensure_store

logger = get_logger("witan.code.index")

# ── Language support ──────────────────────────────────────────────
#
# Adding a language = adding one LanguageSpec: file extensions, the
# tree-sitter grammar name, the .scm query file, and the capture→kind map.

_QUERIES_TS_DIR = Path(__file__).parent / "queries_ts"

# Delete statements per commit when clearing reindexed/purged files. A full-repo
# reindex can touch thousands of files, so the batch has to be capped rather than
# built whole. Two statements per file, so this is 64 files per commit.
#
# ★ THE BINDING CONSTRAINT IS TIME, NOT ARGV. The original cap was 500, justified
# by the composed query riding in a single argv element. That reasoning does not
# apply to the deployed indexer at all: it uses the pooled HTTP transport, where
# the query travels in a JSON body and argv is never involved. Meanwhile a
# MATCHING delete costs ~362ms server-side, so 500 statements is ~181 SECONDS —
# past `witan_core.omnigraph_http.DEFAULT_TIMEOUT_SECONDS` (120s), and the chunk
# fails with a bare "timed out".
#
# That is not hypothetical: it is why `witan-ci-index` failed every run from
# 2026-08-07, in CI and Production alike, always on ol-infrastructure —
# `indexer.py` change_many → chunk of 500 → timeout → "indexed 13, failed 1".
#
# Measured 2026-08-12 against the CI data tier, inserting then deleting real rows
# in `code-github-com-mitodl-lehrer`:
#      50 matching deletes   19.23s   (385 ms/stmt)
#     100 matching deletes   36.29s   (363 ms/stmt)
#     250 matching deletes   90.47s   (362 ms/stmt)
# Linear, and — measured on the SMALLEST graph (1,567 rows) — essentially
# independent of table size, so this is not a big-repo problem. Any repo whose
# reindex touches ~165 files in one run would hit it; ol-infrastructure is simply
# the one busy enough to do so every four hours.
#
# 128 statements is ~46s, a 2.5x margin under the timeout. It does NOT make a
# reindex faster — the cost is linear per statement either way — it makes it
# COMPLETE, which for a four-hourly job is the property that matters.
#
# ★ Raising DEFAULT_TIMEOUT_SECONDS instead would be the wrong lever: that
# budget exists to catch a genuinely wedged server, and widening it to fit a
# known-slow operation blunts the one signal that says the server is stuck.
# The real defect is that a single keyed delete costs 362ms at all — tracked as
# tk-upstream-omnigraph-a-single-row-insert-costs-a-f-eeeae3.
_DELETE_BATCH_SIZE = 128

# Stand-in written into CodeFile.content_hash for the duration of a load, so a
# run that fails part-way leaves nothing the incremental skip check will match.
# Any non-sha256 string does; this one is obvious in a store dump. See
# `_defer_content_hashes`.
_PENDING_CONTENT_HASH = "pending"


@dataclass(frozen=True)
class LanguageSpec:
    name: str
    extensions: tuple[str, ...]
    grammar: str
    # None = no hand-written query: bootstrap from the grammar wheel's own
    # bundled `tags.scm` (see _translate_tags_captures) instead of a file in
    # queries_ts/. Only viable for grammars that ship one (most of the
    # tree-sitter org's own; not community/config grammars like sql/hcl/yaml).
    scm: str | None
    # capture prefix "symbol.<kind>" → Symbol kind
    kinds: dict[str, str]
    # Node kinds that scope nesting/qualified-name resolution (_walk_defs).
    # PER-LANGUAGE, not shared: node-type-name strings collide across
    # unrelated grammars (Python's statement suite is also called "block",
    # which is what HCL calls its resource/variable/… blocks) with very
    # different nesting semantics, so a single global set is unsafe.
    def_node_types: frozenset[str]


# All JS/TS variants use the `tsx` grammar (a superset of TS, JS, and JSX): the
# plain `javascript` grammar rejects the TS node types in typescript.scm.
_TS_KINDS = {
    "function": "function",
    "method": "method",
    "class": "class",
    "interface": "interface",
    "type": "type",
    "enum": "enum",
}

_TS_DEF_NODE_TYPES = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "class_declaration",
        "method_definition",
        "public_field_definition",  # class arrow methods
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
        "variable_declarator",
    }
)

_LANGUAGES: tuple[LanguageSpec, ...] = (
    LanguageSpec(
        name="python",
        extensions=(".py", ".pyi"),
        grammar="python",
        scm="python.scm",
        kinds={"function": "function", "class": "class"},
        def_node_types=frozenset({"function_definition", "class_definition"}),
    ),
    LanguageSpec(
        name="typescript",
        extensions=(".ts", ".mts", ".cts", ".tsx"),
        grammar="tsx",
        scm="typescript.scm",
        kinds=_TS_KINDS,
        def_node_types=_TS_DEF_NODE_TYPES,
    ),
    LanguageSpec(
        name="javascript",
        extensions=(".js", ".jsx", ".mjs", ".cjs"),
        grammar="tsx",
        scm="typescript.scm",
        kinds=_TS_KINDS,
        def_node_types=_TS_DEF_NODE_TYPES,
    ),
    LanguageSpec(
        name="bash",
        extensions=(".sh", ".bash", ".zsh"),
        grammar="bash",
        scm="bash.scm",
        kinds={"function": "function"},
        def_node_types=frozenset({"function_definition"}),
    ),
    LanguageSpec(
        name="yaml",
        extensions=(".yaml", ".yml"),
        grammar="yaml",
        scm="yaml.scm",
        kinds={"key": "key"},
        def_node_types=frozenset({"block_mapping_pair"}),
    ),
    LanguageSpec(
        name="go",
        extensions=(".go",),
        grammar="go",
        scm=None,  # bootstrapped from tree_sitter_go's bundled tags.scm
        kinds={"function": "function", "method": "method", "type": "type"},
        def_node_types=frozenset(
            {"function_declaration", "method_declaration", "type_spec"}
        ),
    ),
    LanguageSpec(
        name="sql",
        extensions=(".sql",),
        grammar="sql",
        scm="sql.scm",
        kinds={"table": "table", "function": "function", "cte": "cte"},
        def_node_types=frozenset(
            {"create_table", "create_view", "create_function", "cte"}
        ),
    ),
    LanguageSpec(
        name="hcl",
        extensions=(".hcl", ".tf"),
        grammar="hcl",
        scm="hcl.scm",
        kinds={"block": "block"},
        def_node_types=frozenset({"block"}),
    ),
)

_EXT_TO_SPEC: dict[str, LanguageSpec] = {
    ext: spec for spec in _LANGUAGES for ext in spec.extensions
}

# Standalone tree-sitter grammar wheels (no language-pack): grammar name → the
# (module, factory) that yields the compiled grammar capsule. Adding a language =
# add its `tree-sitter-<lang>` wheel to pyproject + an entry here.
_GRAMMAR_MODULES: dict[str, tuple[str, str]] = {
    "python": ("tree_sitter_python", "language"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
    "bash": ("tree_sitter_bash", "language"),
    "yaml": ("tree_sitter_yaml", "language"),
    "go": ("tree_sitter_go", "language"),
    "sql": ("tree_sitter_sql", "language"),
    "hcl": ("tree_sitter_hcl", "language"),
}


@functools.lru_cache(maxsize=None)
def _ts_language(grammar: str):
    """Build (and cache) a ``tree_sitter.Language`` from its standalone wheel."""
    from tree_sitter import Language

    module_name, factory = _GRAMMAR_MODULES[grammar]
    module = importlib.import_module(module_name)
    return Language(getattr(module, factory)())


_SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".next",
}


# ── Extracted-symbol records ──────────────────────────────────────


@dataclass
class ParsedSymbol:
    id: str
    name: str
    qualified_name: str
    kind: str
    start_line: int
    end_line: int
    signature: str | None
    docstring: str | None
    decorators: list[str] | None = None


@dataclass
class ParsedFile:
    file_id: str
    path: str
    language: str
    content_hash: str
    symbols: list[ParsedSymbol] = field(default_factory=list)
    # (container_qualified_name | None, child_qualified_name) for Contains
    contains: list[tuple[str | None, str]] = field(default_factory=list)
    # raw call/reference identifier names seen in the file
    call_names: set[str] = field(default_factory=set)
    # (enclosing_qualified_name, call_identifier_name) for precise Calls edges
    calls: list[tuple[str, str]] = field(default_factory=list)
    # base class identifier names per class qualified_name
    inherits: dict[str, list[str]] = field(default_factory=dict)
    # imported identifier names
    imports: set[str] = field(default_factory=set)
    # cross-repo interface bindings (env vars, packages, endpoints) in this file
    bindings: list[ParsedBinding] = field(default_factory=list)


@dataclass
class IndexStats:
    scanned: int = 0
    indexed: int = 0
    skipped: int = 0
    symbols: int = 0
    edges: int = 0
    bindings: int = 0
    errors: int = 0
    bridge_failed: bool = False
    """The cross-repo bridge write raised, so ``bindings`` is not a count.

    Without this, ``bindings=0`` means both "nothing to write" and "the write
    threw", and the summary line is the only thing most people read. On
    production those two states were indistinguishable for 15 hours.
    """
    purged: int = 0
    """Files dropped from the store because they are no longer part of the
    repo — deleted, or newly excluded (a nested checkout, a skipped
    directory). Full-repo runs only; reported so a mass cleanup is visible
    rather than silent."""


class IndexFailed(RuntimeError):
    """A write phase of :func:`index_path` failed, carrying what it knows.

    ★ THE FAILING RUN IS THE ONE THAT REPORTED LEAST. A successful index prints
    ``scanned=… indexed=… skipped=… symbols=…``; a failing one printed only the
    exception, because the exception replaced the summary — so the run you most
    need data from was the only one that gave you none. That is why the CI
    indexer's ``ol-infrastructure`` timeout sat unexplained for five days
    (tk-the-ci-indexer-s-failure-says-only-timed-out-no--50f61c): attributing it
    needed a traceback line number, a source read to find the constant, row
    counts from three graphs and a live timing experiment, almost all of which
    the numbers below would have answered directly.

    ``stats`` is the partial :class:`IndexStats` as of the failure, ``phase``
    names the write that failed, and ``detail`` carries the sizes that phase was
    working with. ``elapsed`` is the seconds that phase ran before dying, which
    is what says whether a timeout was hit or something failed early.
    """

    def __init__(
        self,
        phase: str,
        cause: BaseException,
        *,
        stats: IndexStats,
        elapsed: float,
        detail: dict[str, object],
    ) -> None:
        self.phase = phase
        self.stats = stats
        self.elapsed = elapsed
        self.detail = detail
        described = " ".join(f"{k}={v}" for k, v in detail.items())
        super().__init__(
            f"{phase} failed after {elapsed:.1f}s"
            + (f" ({described})" if described else "")
            + f": {cause}"
        )


@contextlib.contextmanager
def _write_phase(phase: str, stats: IndexStats, **detail: object) -> Iterator[None]:
    """Re-raise anything from a write as :class:`IndexFailed` with its sizes."""
    started = time.monotonic()
    try:
        yield
    except Exception as exc:
        raise IndexFailed(
            phase,
            exc,
            stats=stats,
            elapsed=time.monotonic() - started,
            detail=detail,
        ) from exc


# ── Public entry points ───────────────────────────────────────────


def index_path(
    target: Path,
    *,
    force: bool = False,
    repo_override: str | None = None,
    config: cfg_module.Config | None = None,
) -> IndexStats:
    """Index ``target`` (a file or directory) into the repo's code store.

    Incremental by default: unchanged files (matching content_hash) are skipped.
    ``force`` re-indexes regardless of hash.
    """
    cfg = config or cfg_module.load()
    target = target.resolve()

    repo_root = repo_module.root(target if target.is_dir() else target.parent)
    slug = repo_module.detect(override=repo_override, start=repo_root or target)
    if slug is None:
        # No git context: use the directory name of the target.
        slug = (target if target.is_dir() else target.parent).name
    base = repo_root or (target if target.is_dir() else target.parent)

    # Non-default git branches index onto their own omnigraph branch, forked
    # from main on first write (docs/BRANCH_INDEXING.md), so in-flight work
    # never overwrites the shared main view and stays visible per-branch. The
    # view is named for its writer as well as the git branch
    # (:mod:`witan_code.views`): on a shared graph the git branch alone is not
    # a unique key — two checkouts on `feature-x` are two working trees — and
    # the un-namespaced name is what let them overwrite each other.
    actor = identity_module.actor_id()
    git_branch = repo_module.store_branch(base) if repo_root else None
    branch = views.repo_view(git_branch, actor=actor) if git_branch else None

    store = ensure_store(slug, cfg)
    client = store.client(cfg, branch=branch)
    check_writable(
        is_remote=client.is_remote, branch=branch, cfg=cfg, slug=slug, actor=actor
    )
    # The hash read below never forks; create the branch before reading.
    client.ensure_branch()

    # One query for all existing file hashes → the incremental skip check is
    # in-memory, not a query per file. Read even under `force` (where the
    # hashes go unused): a full-repo run also needs the stored file set to
    # find rows for files that are no longer part of the repo.
    existing: dict[str, str] = {}
    for row in client.read("code_read.gq", "all_file_hashes", {}):
        existing[row["slug"]] = row.get("content_hash")

    stats = IndexStats()
    records: list[dict] = []
    reindexed_file_ids: list[str] = []
    bindings: list[ParsedBinding] = []
    touched_files: list[str] = []

    # A directory the walk could not read makes `collected` incomplete in a way
    # that is invisible from the result alone — and the purge below reads
    # "missing from the collected set" as "no longer part of the repo".
    walk_errors: list[OSError] = []
    collected = _collect_files(target, on_error=walk_errors.append)
    for exc in walk_errors:
        stats.errors += 1
        # Warning: an unreadable directory silently shrinks `collected`, and
        # the purge below reads "missing from collected" as "deleted from the
        # repo" — so this is the difference between a partial index and a
        # wrongly-purged one.
        logger.warning("witan.code.index.walk_error", path=exc.filename, error=str(exc))

    # Every file the repo should have indexed, changed or not — the authority
    # for the purge below, which `touched_files` is not (an incremental run
    # touches only what changed).
    indexed_rel = {_relative_path(p, base) for p in collected}

    for path in collected:
        stats.scanned += 1
        try:
            result = _parse_for_index(path, base, slug, existing, force=force)
        except Exception as exc:  # noqa: BLE001 — one bad file must not abort
            stats.errors += 1
            logger.warning(
                "witan.code.index.file_failed",
                path=str(path),
                repo=slug,
                error=str(exc),
                exc_info=True,
            )
            continue
        if result is None:
            stats.skipped += 1
            continue
        parsed, was_existing = result
        if was_existing:
            reindexed_file_ids.append(parsed.file_id)
        records.extend(_file_records(parsed, slug, stats))
        bindings.extend(parsed.bindings)
        touched_files.append(parsed.path)
        stats.indexed += 1

    full_repo = target.is_dir() and target.resolve() == base.resolve()
    can_purge = _may_purge(
        full_repo=full_repo,
        repo_root=repo_root,
        walk_errors=walk_errors,
        client=client,
        branch=branch,
        cfg=cfg,
        actor=actor,
    )

    # Drop stale data for changed files (new files have nothing to delete), then
    # bulk-load every node and edge in a single omnigraph call. The deletes are
    # collected rather than issued per file: two commits per changed file made a
    # reindex the single largest fragmentation source in the store, which is
    # exactly what the bulk load below exists to avoid on the insert side.
    delete_steps: list[tuple[str, str, dict]] = []
    for file_id in reindexed_file_ids:
        delete_steps += _delete_file_steps(file_id)

    # A full-repo run also drops files the repo no longer has. Membership is
    # decided by the collected set, NOT by whether the file still exists on
    # disk: a linked worktree's files are very much on disk, they simply
    # aren't this repo's to index (see `_is_nested_checkout`). Without this,
    # excluding a path stops adding rows but leaves every row already written
    # — which is how a store ends up majority stale copies of itself.
    # Subpath runs are exempt: everything outside the subpath is legitimately
    # absent from `indexed_rel` and must not be treated as stale.
    if can_purge:
        prefix = f"{slug}#"
        for file_id in sorted(existing):
            rel = file_id[len(prefix) :] if file_id.startswith(prefix) else None
            if rel is not None and rel not in indexed_rel:
                delete_steps += _delete_file_steps(file_id)
                stats.purged += 1

    with _write_phase(
        "delete of stale rows",
        stats,
        statements=len(delete_steps),
        chunk_size=_DELETE_BATCH_SIZE,
        reindexed_files=len(reindexed_file_ids),
        purged_files=stats.purged,
    ):
        client.change_many(delete_steps, chunk_size=_DELETE_BATCH_SIZE)
    # Two loads, not one: the second commits the content hashes the first held
    # back, so a load that dies part-way is re-done rather than skipped forever.
    # See `_defer_content_hashes`.
    deduped = _dedupe(records)
    real_hash_rows = _defer_content_hashes(deduped)
    with _write_phase("load of nodes and edges", stats, records=len(deduped)):
        client.load(deduped, mode="merge")
    with _write_phase("load of content hashes", stats, records=len(real_hash_rows)):
        client.load(real_hash_rows, mode="merge")

    # Cross-repo bridge — a SEPARATE phase after the per-repo store write, so the
    # two stores' write locks never nest. A full-repo index (target is the repo
    # root) also runs the repo-level provider extractors and clears bindings for
    # files deleted from disk; all purging is per-file so unchanged (skipped)
    # files keep their bindings.
    #
    # A non-default branch targets its repo-qualified bridge branch overlay
    # (docs/BRANCH_INDEXING.md § Bridge store) rather than skipping the bridge
    # entirely: the shared main view still never sees in-flight bindings, but
    # they're no longer dropped on the floor either.
    if full_repo:
        bindings.extend(bridge_extractors.extract_repo_bindings(base, slug))
    try:
        stats.bindings = bridge_module.write_bindings(
            bindings,
            slug,
            cfg,
            full_repo=full_repo,
            touched_files=tuple(touched_files),
            identity=package_map.load(base, slug),
            base=base,
            branch=git_branch,
            actor=actor,
            indexed_files=frozenset(indexed_rel) if can_purge else None,
        )
    except Exception as exc:  # noqa: BLE001 — bridge is best-effort, never fatal
        # ERROR, not warning, and the level is the whole mechanism. Sentry's
        # LoggingIntegration is installed with `event_level=ERROR` precisely so
        # a site like this needs no `capture_exception` call — but that also
        # means a warning here was, by that same contract, a declaration that
        # the failure is "expected and already handled". A bridge write that
        # throws is neither.
        #
        # It cost 15 hours of silently-frozen cross-repo bindings on production
        # to find that out: every CI cycle logged this line, reported
        # `bindings=0 errors=0`, exited 0, and raised nothing anywhere. See
        # witan_core.observability.telemetry.configure_sentry.
        #
        # Still not fatal — the per-repo index above IS written and is worth
        # keeping — but "non-fatal" and "unreported" are different claims, and
        # this only ever meant the first.
        stats.errors += 1
        stats.bridge_failed = True
        logger.error(
            "witan.code.index.bridge_failed", repo=slug, error=str(exc), exc_info=True
        )

    return stats


def _defer_content_hashes(records: list[dict]) -> list[dict]:
    """Hold every CodeFile's ``content_hash`` back until the load has landed.

    Mutates the CodeFile rows in ``records`` to carry ``_PENDING_CONTENT_HASH``
    and returns copies carrying the REAL hashes, to be loaded once the main load
    succeeds.

    WHY THIS EXISTS. ``index_path`` deletes a changed file's rows and reloads
    them. While that reload was one atomic ``load``, a failure left the file
    with no recorded hash at all, so the next run re-indexed it — failure was
    self-healing, for free. Chunking the load (``chunking.chunk_records``) takes
    that away: the nodes land, hash and all, in an early batch, and if a later
    batch fails then ``_parse_for_index``'s ``existing.get(file_id) ==
    content_hash`` check SKIPS the file on every subsequent run. Its symbols
    would be present and its edges permanently missing, silently, until someone
    thought to force a full reindex. Chunking also raises the odds of a
    part-way failure, since it turns one request into several.

    The sentinel restores the old behaviour: it can never equal a real hash (a
    64-char sha256 hexdigest), so a file whose hashes were never committed is
    simply re-indexed. Nothing else reads ``content_hash`` — only this check and
    the informational ``get_file`` query — so a row briefly carrying the
    sentinel is accurate: that file does need re-indexing.
    """
    real_rows: list[dict] = []
    for record in records:
        if record.get("type") != "CodeFile":
            continue
        data = record["data"]
        real_rows.append({"type": "CodeFile", "data": dict(data)})
        data["content_hash"] = _PENDING_CONTENT_HASH
    return real_rows


def _dedupe(records: list[dict]) -> list[dict]:
    """Drop duplicate node slugs / edges so one collision can't fail the load.

    Real code yields occasional duplicate qualified names (overloads, a def named
    after its file). Omnigraph's load rejects the whole batch on a single
    ``@unique`` violation, so keep the first occurrence of each node slug and edge.
    """
    seen_nodes: set[str] = set()
    seen_edges: set[tuple[str, str, str]] = set()
    out: list[dict] = []
    for record in records:
        if "type" in record:
            slug = record["data"]["slug"]
            if slug in seen_nodes:
                continue
            seen_nodes.add(slug)
        else:
            key = (record["edge"], record["from"], record["to"])
            if key in seen_edges:
                continue
            seen_edges.add(key)
        out.append(record)
    return out


def _may_purge(
    *,
    full_repo: bool,
    repo_root: Path | None,
    walk_errors: list[OSError],
    client: OmnigraphClient,
    branch: str | None,
    cfg: cfg_module.Config,
    actor: str | None,
) -> bool:
    """Whether this run is entitled to delete rows for files it did not collect.

    Purging is the one destructive thing an index does, so it asks for more
    than the run being a full-repo one. Every clause answers the same question
    — is this machine's file listing authoritative for the view being written?

    - ``full_repo``: a subpath run legitimately collects a fraction of the repo.
    - ``repo_root``: without a git root, ``base`` falls back to the target
      directory, making ``full_repo`` true for ANY directory — index a
      subdirectory then and every path stored relative to the real root looks
      stale.
    - ``walk_errors``: a directory the walk could not read is indistinguishable
      from one that was deleted, so its files would go while sitting on disk.
    - :func:`~witan_code.graph.owns_view`: the same predicate that decides
      whether this process may WRITE the view at all, because deleting from a
      view you do not own is the more destructive half of that. It answers
      "CI owns the shared default view" and "an actor owns its own branch
      views" in one place — the earlier "remote and not the designated writer"
      rule got the first right and the second wrong, refusing a developer the
      purge of their OWN branch view, where deleted files therefore lingered.
    """
    return (
        full_repo
        and repo_root is not None
        and not walk_errors
        and owns_view(is_remote=client.is_remote, branch=branch, cfg=cfg, actor=actor)
    )


def _relative_path(path: Path, base: Path) -> str:
    """A file's repo-relative path — the second half of its ``repo#rel`` id.

    Shared by the parse path and the stale-file purge so the two agree on what
    identifies a file; a mismatch there would purge rows that were just written.
    """
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.name


def _is_nested_checkout(path: Path) -> bool:
    """Whether ``path`` is the root of a *different* git checkout.

    A `.git` entry inside a subdirectory means that subtree belongs to another
    repository — a linked worktree (`.claude/worktrees/<name>/`, where `.git`
    is a *file* pointing back at the parent's `.git/worktrees/`), a submodule
    (also a `.git` file), or a plain clone someone dropped in. Its files are
    that repo's, and indexing them here attributes them to this one.
    """
    return (path / ".git").exists()


def _collect_files(
    target: Path, *, on_error: Callable[[OSError], None] | None = None
) -> list[Path]:
    """Every indexable file under ``target``, sorted.

    ``on_error`` is handed any directory that could not be read. ``os.walk``
    swallows those silently by default, which is fine for indexing (skip what
    you can't read) but NOT for the purge that consumes this list: an
    unreadable subtree would look like a set of deleted files and take their
    rows with it. ``index_path`` passes a collector and declines to purge when
    anything fired.
    """
    if target.is_file():
        return [target] if target.suffix in _EXT_TO_SPEC else []

    out: list[Path] = []
    for root, dirs, files in os.walk(target, onerror=on_error):
        root_path = Path(root)
        # Prune in place, so a skipped directory is never descended into at
        # all. The previous rglob("*") walked every file under node_modules/
        # .venv/.git and discarded them afterwards; this also makes the
        # nested-checkout test cheap, since it runs once per surviving
        # directory rather than once per file.
        #
        # `target` itself is never a pruning candidate (only entries in
        # `dirs` are), so indexing a repo root — or a path *inside* a
        # worktree, which is how the hooks index while an agent works there —
        # still works. Only descending into one from outside is refused.
        dirs[:] = [
            d
            for d in dirs
            if d not in _SKIP_DIRS and not _is_nested_checkout(root_path / d)
        ]
        for name in files:
            path = root_path / name
            if path.suffix in _EXT_TO_SPEC:
                out.append(path)
    # os.walk's directory order is arbitrary; sort so a run is reproducible
    # and `_dedupe`'s first-wins tie-break is stable across runs.
    return sorted(out)


# ── Per-file indexing ─────────────────────────────────────────────


def _parse_for_index(
    path: Path,
    base: Path,
    slug: str,
    existing: dict[str, str],
    *,
    force: bool,
) -> tuple[ParsedFile, bool] | None:
    """Parse ``path`` unless unchanged. Returns (parsed, file_already_indexed)."""
    spec = _EXT_TO_SPEC[path.suffix]
    raw = path.read_bytes()
    content_hash = hashlib.sha256(raw).hexdigest()

    rel = _relative_path(path, base)
    file_id = f"{slug}#{rel}"

    if not force and existing.get(file_id) == content_hash:
        return None  # unchanged

    parsed = _parse_file(raw, path, spec, slug, file_id, rel, content_hash)
    if parsed is None:
        return None
    return parsed, (file_id in existing)


def _delete_file_steps(file_id: str) -> list[tuple[str, str, dict]]:
    """The two deletes that clear one file's rows, as batchable steps.

    Deletes must not be mixed with inserts — the engine rejects a body that is
    both constructive and destructive — which is why these stay separate from
    the ``load()`` that follows. They batch freely with OTHER deletes though, so
    every file's deletes go into one commit rather than two apiece.
    """
    return [
        ("delete.gq", "delete_symbols_in_file", {"file_id": file_id}),
        ("delete.gq", "delete_file", {"id": file_id}),
    ]


def _edge(edge_type: str, from_id: str, to_id: str) -> dict:
    return {"edge": edge_type, "from": from_id, "to": to_id}


def _file_records(parsed: ParsedFile, slug: str, stats: IndexStats) -> list[dict]:
    """Build the load() records (node + edge JSONL dicts) for one parsed file."""
    now = now_iso()
    records: list[dict] = [
        {
            "type": "CodeFile",
            "data": {
                "slug": parsed.file_id,
                "repo": slug,
                "path": parsed.path,
                "language": parsed.language,
                "content_hash": parsed.content_hash,
                "indexed_at": now,
            },
        }
    ]

    by_qualified: dict[str, ParsedSymbol] = {}
    by_name: dict[str, list[ParsedSymbol]] = {}
    for sym in parsed.symbols:
        records.append(
            {
                "type": "Symbol",
                "data": {
                    "slug": sym.id,
                    "repo": slug,
                    "file_id": parsed.file_id,
                    "name": sym.name,
                    "qualified_name": sym.qualified_name,
                    "kind": sym.kind,
                    "start_line": sym.start_line,
                    "end_line": sym.end_line,
                    "signature": sym.signature,
                    "docstring": sym.docstring,
                    "decorators": sym.decorators,
                    "indexed_at": now,
                },
            }
        )
        stats.symbols += 1
        records.append(_edge("Defines", parsed.file_id, sym.id))
        stats.edges += 1
        by_qualified[sym.qualified_name] = sym
        by_name.setdefault(sym.name, []).append(sym)

    # Contains: lexical nesting within this file.
    for container_qn, child_qn in parsed.contains:
        if container_qn and container_qn in by_qualified and child_qn in by_qualified:
            records.append(
                _edge(
                    "Contains", by_qualified[container_qn].id, by_qualified[child_qn].id
                )
            )
            stats.edges += 1

    # Heuristic Calls/References: each call identifier is attributed to the
    # qualified name of its nearest enclosing definition (computed at parse
    # time) and resolved to a same-file symbol by name. Falls back to a stable
    # file-level origin when the enclosing def isn't itself a known symbol.
    fallback = _reference_origin(parsed)
    seen_calls: set[tuple[str, str]] = set()
    for origin_qn, cname in parsed.calls:
        origin = by_qualified.get(origin_qn) or fallback
        if origin is None:
            continue
        target = _resolve_local(cname, by_name)
        if target is None or target.id == origin.id:
            continue
        if (origin.id, target.id) in seen_calls:
            continue
        seen_calls.add((origin.id, target.id))
        records.append(_edge("Calls", origin.id, target.id))
        records.append(_edge("References", origin.id, target.id))
        stats.edges += 2

    # Heuristic Inherits: resolve base class names to same-file class symbols.
    for class_qn, bases in parsed.inherits.items():
        child = by_qualified.get(class_qn)
        if child is None:
            continue
        for base_name in bases:
            target = _resolve_local(base_name, by_name)
            if target is not None and target.id != child.id:
                records.append(_edge("Inherits", child.id, target.id))
                stats.edges += 1

    # Heuristic Imports: resolve imported names to same-file symbols (best-effort;
    # cross-file resolution is left to query-time lookups by name).
    for iname in parsed.imports:
        target = _resolve_local(iname, by_name)
        if target is not None:
            records.append(_edge("Imports", parsed.file_id, target.id))
            stats.edges += 1

    return records


def _reference_origin(parsed: ParsedFile) -> ParsedSymbol | None:
    """Pick a stable symbol to attribute file-level references to.

    Prefers the first top-level (non-nested) symbol; falls back to the first
    symbol overall. Returns None for empty files.
    """
    nested = {child for _, child in parsed.contains}
    for sym in parsed.symbols:
        if sym.qualified_name not in nested:
            return sym
    return parsed.symbols[0] if parsed.symbols else None


def _resolve_local(
    name: str, by_name: dict[str, list[ParsedSymbol]]
) -> ParsedSymbol | None:
    matches = by_name.get(name)
    return matches[0] if matches else None


# ── Parsing ───────────────────────────────────────────────────────


def _parse_file(
    raw: bytes,
    path: Path,
    spec: LanguageSpec,
    slug: str,
    file_id: str,
    rel: str,
    content_hash: str,
) -> ParsedFile | None:
    from tree_sitter import Parser

    language = _ts_language(spec.grammar)
    # The Parser/Query/QueryCursor all come from the standalone `tree_sitter`
    # package bound to this Language. parse() wants bytes; _node_text slices into
    # the same bytes.
    parser = Parser(language)
    tree = parser.parse(raw)
    root = _root(tree)

    parsed = ParsedFile(
        file_id=file_id,
        path=rel,
        language=spec.name,
        content_hash=content_hash,
    )

    # Module-level symbol (the file itself as a module). Its qualified_name uses
    # a sentinel so a top-level def named after the file (e.g. `def foo` in
    # foo.py) doesn't collide with the module on `slug`.
    module_name = Path(rel).stem
    module = ParsedSymbol(
        id=f"{file_id}::<module>",
        name=module_name,
        qualified_name="<module>",
        kind="module",
        start_line=1,
        end_line=_end_line(root),
        signature=None,
        docstring=None,
    )
    parsed.symbols.append(module)

    # Definition nodes (functions/classes/methods/…): walk the tree so we can
    # compute lexical qualified names and Contains nesting.
    def_capture_nodes = _query_captures(language, spec, root)

    _walk_defs(root, raw, spec, file_id, module, parsed, def_capture_nodes)

    # Imports gathered flat. Calls are attributed to their enclosing def inside
    # _walk_defs (which has the qualified-name machinery); inherit.base likewise.
    for cap_name, node in def_capture_nodes:
        if cap_name.startswith("import."):
            parsed.imports.add(_node_text(node, raw))

    # Cross-repo interface bindings (env vars, packages, endpoint consumers).
    # Attribute each to its enclosing symbol by line containment.
    parsed.bindings = bridge_extractors.extract_file_bindings(
        raw.decode("utf-8", "replace"), spec.name, rel
    )
    for binding in parsed.bindings:
        binding.symbol_id = _symbol_at_line(parsed, binding.line)

    return parsed


def _symbol_at_line(parsed: ParsedFile, line: int | None) -> str | None:
    """The smallest non-module symbol whose range contains ``line``.

    Falls back to the module symbol so every binding has a stable owner.
    """
    if line is None:
        return parsed.symbols[0].id if parsed.symbols else None
    best: ParsedSymbol | None = None
    for sym in parsed.symbols:
        if sym.qualified_name == "<module>":
            continue
        if sym.start_line <= line <= sym.end_line:
            if best is None or (sym.end_line - sym.start_line) < (
                best.end_line - best.start_line
            ):
                best = sym
    if best is not None:
        return best.id
    return parsed.symbols[0].id if parsed.symbols else None


def _walk_defs(
    root,
    raw: bytes,
    spec: LanguageSpec,
    file_id: str,
    module: ParsedSymbol,
    parsed: ParsedFile,
    captures: list[tuple[str, object]],
) -> None:
    # Map each captured definition-name node key → (kind, name_text). HCL's
    # captured name is the block's `string_lit` label node (quotes included,
    # since the label isn't a direct child otherwise reachable — see
    # queries_ts/hcl.scm); strip the quotes there only. No other language
    # captures a string_lit as its name, so this can't affect them.
    name_nodes: dict[tuple, tuple[str, str]] = {}
    for cap_name, node in captures:
        if cap_name.startswith("symbol."):
            kind = spec.kinds.get(cap_name.split(".", 1)[1])
            if kind:
                text = _node_text(node, raw)
                if spec.name == "hcl" and _kind(node) == "string_lit":
                    text = text.strip("\"'")
                name_nodes[_node_key(node)] = (kind, text)

    def enclosing_def(node):
        cur = _parent(node)
        while cur is not None:
            if _kind(cur) in spec.def_node_types:
                return cur
            cur = _parent(cur)
        return None

    def def_name_node(def_node):
        for child in _children(def_node):
            if _node_key(child) in name_nodes:
                return child
        # variable_declarator: name field
        nf = _child_by_field_name(def_node, "name")
        if nf is not None and _node_key(nf) in name_nodes:
            return nf
        return None

    # Build qualified names by ascending the def hierarchy.
    def qualified(def_node) -> tuple[str, str, str] | None:
        nn = def_name_node(def_node)
        if nn is None:
            return None
        kind, name = name_nodes[_node_key(nn)]
        parts = [name]
        parent_def = enclosing_def(def_node)
        while parent_def is not None:
            pnn = def_name_node(parent_def)
            if pnn is not None:
                parts.append(name_nodes[_node_key(pnn)][1])
            parent_def = enclosing_def(parent_def)
        parts.reverse()
        return kind, name, ".".join(parts)

    seen: set[str] = set()
    for cap_name, node in captures:
        if not cap_name.startswith("symbol."):
            continue
        def_node = _parent(node)
        while def_node is not None and _kind(def_node) not in spec.def_node_types:
            def_node = _parent(def_node)
        if def_node is None:
            continue
        q = qualified(def_node)
        if q is None:
            continue
        kind, name, qn = q
        if qn in seen:
            continue
        seen.add(qn)

        sym = ParsedSymbol(
            id=f"{file_id}::{qn}",
            name=name,
            qualified_name=qn,
            kind=kind,
            start_line=_start_line(def_node),
            end_line=_end_line(def_node),
            signature=_signature(def_node, raw),
            docstring=_docstring(def_node, raw, spec),
            decorators=_decorators(def_node, raw, spec),
        )
        parsed.symbols.append(sym)

        parent_def = enclosing_def(def_node)
        parent_q = qualified(parent_def) if parent_def is not None else None
        container_qn = parent_q[2] if parent_q else module.qualified_name
        parsed.contains.append((container_qn, qn))

        # Inherits: base identifiers within this class def.
        if kind == "class":
            bases = _class_bases(def_node, raw, captures)
            if bases:
                parsed.inherits[qn] = bases

    # Attribute each call to the qualified name of its nearest enclosing def
    # (falls back to the module symbol for top-level calls).
    for cap_name, node in captures:
        if not cap_name.startswith("call."):
            continue
        cname = _node_text(node, raw)
        parsed.call_names.add(cname)
        def_node = enclosing_def(node)
        origin_qn = module.qualified_name
        if def_node is not None:
            q = qualified(def_node)
            if q is not None:
                origin_qn = q[2]
        parsed.calls.append((origin_qn, cname))


def _class_bases(def_node, raw: bytes, captures) -> list[str]:
    bases: list[str] = []
    for cap_name, node in captures:
        if cap_name != "inherit.base":
            continue
        target_key = _node_key(def_node)
        cur = _parent(node)
        while cur is not None:
            if _node_key(cur) == target_key:
                bases.append(_node_text(node, raw))
                break
            cur = _parent(cur)
    return bases


# ── tree-sitter helpers ───────────────────────────────────────────


def _query_captures(language, spec: LanguageSpec, root) -> list[tuple[str, object]]:
    from tree_sitter import Query

    if spec.scm is not None:
        scm = (_QUERIES_TS_DIR / spec.scm).read_text()
        bootstrapped = False
    else:
        scm = _tags_query_text(spec)
        bootstrapped = True

    try:
        query = Query(language, scm)
    except Exception:  # noqa: BLE001 — fall back to Language.query
        query = language.query(scm)

    # The capture API moved across py-tree-sitter versions: 0.23+ exposes
    # QueryCursor whose captures() returns {name: [nodes]}; older versions had
    # Query.captures() returning [(node, name)] tuples. Support both.
    try:
        from tree_sitter import QueryCursor

        raw = QueryCursor(query).captures(root)
    except ImportError:
        raw = query.captures(root)

    out: list[tuple[str, object]] = []
    if isinstance(raw, dict):
        for cap_name, nodes in raw.items():
            out.extend((cap_name, node) for node in nodes)
    else:
        for node, cap_name in raw:
            out.append((cap_name, node))
    return _translate_tags_captures(out) if bootstrapped else out


def _tags_query_text(spec: LanguageSpec) -> str:
    """Query text for a language with no hand-written queries_ts/*.scm.

    Bootstrapped from the grammar wheel's own bundled ``tags.scm`` — the
    standard tree-sitter "code navigation" query convention (captures
    ``@definition.<kind>`` / ``@name`` / ``@reference.call``), shipped as
    package data and exposed as ``TAGS_QUERY`` by most tree-sitter-org
    grammars (see docs/LANGUAGE_SUPPORT.md). Not every grammar ships one —
    notably config/declarative grammars (sql, hcl, yaml, dockerfile) don't,
    so those still need a hand-written queries_ts/*.scm like python.scm.
    """
    module_name, _ = _GRAMMAR_MODULES[spec.grammar]
    module = importlib.import_module(module_name)
    tags_query = getattr(module, "TAGS_QUERY", None)
    if tags_query is None:
        raise ValueError(
            f"{spec.name}: no queries_ts/{spec.name}.scm and grammar module "
            f"{module_name!r} bundles no TAGS_QUERY — write a hand-written "
            f"query file instead of leaving LanguageSpec.scm=None"
        )
    return tags_query


def _translate_tags_captures(
    captures: list[tuple[str, object]],
) -> list[tuple[str, object]]:
    """Adapt the standard tags.scm convention onto witan's own captures.

    tags.scm wraps a name: ``(function_definition name: (identifier) @name)
    @definition.function`` — the kind lives on the outer definition/reference
    node, while ``@name`` sits on the identifier itself. witan's own
    hand-written queries instead put the kind directly on the identifier
    (``@symbol.function`` / ``@call.name``), which is what ``_walk_defs``
    expects. Reattach each ``@name`` node to its smallest enclosing
    ``@definition.*``/``@reference.call`` wrapper to bridge the two.

    A ``@name`` node with no enclosing wrapper (e.g. tags.scm's bare
    top-level `@name` on imports/package clauses/var decls) is dropped —
    those aren't modeled as witan Symbols today.
    """
    name_nodes = [node for cap, node in captures if cap == "name"]
    wrappers: list[tuple[str, object]] = []
    for cap, node in captures:
        if cap.startswith("definition."):
            wrappers.append((f"symbol.{cap.split('.', 1)[1]}", node))
        elif cap == "reference.call":
            wrappers.append(("call.name", node))

    out: list[tuple[str, object]] = []
    for name_node in name_nodes:
        best: tuple[str, int] | None = None
        for cap_name, wrapper in wrappers:
            if not _contains(wrapper, name_node):
                continue
            size = _end_byte(wrapper) - _start_byte(wrapper)
            if best is None or size < best[1]:
                best = (cap_name, size)
        if best is not None:
            out.append((best[0], name_node))
    return out


def _contains(container, node) -> bool:
    return _start_byte(container) <= _start_byte(node) and _end_byte(node) <= _end_byte(
        container
    )


def _a(obj, name, *args):
    """Resolve attribute-or-zero/one-arg-method, version-robustly.

    In tree-sitter 0.25 (Rust/pyo3) Node members (`kind`, `byte_range`,
    `start_byte`, `child`, `parent`, …) are zero/one-arg methods; in the
    classic C binding they were plain attributes. Call when callable.
    """
    val = getattr(obj, name)
    return val(*args) if callable(val) else val


def _root(tree):
    return _a(tree, "root_node")


def _kind(node) -> str:
    # tree_sitter 0.25 Node exposes `.type`; the pack binding exposes `.kind`.
    # Both attrs may exist but one returns None — prefer whichever is set.
    return _a(node, "type") or _a(node, "kind")


def _parent(node):
    return _a(node, "parent")


def _prev_sibling(node):
    return _a(node, "prev_sibling")


def _start_byte(node) -> int:
    return _a(node, "start_byte")


def _end_byte(node) -> int:
    return _a(node, "end_byte")


def _child_by_field_name(node, field: str):
    return _a(node, "child_by_field_name", field)


def _children(node) -> list:
    children = getattr(node, "children", None)
    if children is not None and not callable(children):
        return list(children)
    count = _a(node, "child_count")
    return [_a(node, "child", i) for i in range(count)]


def _node_key(node):
    """Hashable identity for a node (no `.id` in 0.25): use byte range."""
    return (_start_byte(node), _end_byte(node))


def _point(node, which: str):
    # tree_sitter 0.25 Node: `.start_point`/`.end_point` (Point attrs).
    # pack binding: `.start_position`/`.end_position` (callable).
    p = getattr(node, f"{which}_point", None)
    if p is None:
        p = _a(node, f"{which}_position")
    return p


def _start_line(node) -> int:
    return _point(node, "start").row + 1


def _end_line(node) -> int:
    return _point(node, "end").row + 1


def _node_text(node, raw: bytes) -> str:
    return raw[_start_byte(node) : _end_byte(node)].decode("utf-8", "replace")


def _signature(def_node, raw: bytes) -> str | None:
    """The definition header — name + full (multi-line) params + return type.

    Everything from the def start up to its body, whitespace-collapsed, with the
    trailing block opener (``:`` / ``{``) dropped. Falls back to the first line
    when there's no body field (e.g. arrow consts, yaml keys).
    """
    body = _child_by_field_name(def_node, "body")
    if body is not None:
        header = raw[_start_byte(def_node) : _start_byte(body)].decode(
            "utf-8", "replace"
        )
    else:
        lines = _node_text(def_node, raw).splitlines()
        header = lines[0] if lines else ""
    sig = " ".join(header.split()).rstrip()
    if sig.endswith(("{", ":")):
        sig = sig[:-1].rstrip()
    return sig[:300] or None


def _docstring(def_node, raw: bytes, spec: LanguageSpec) -> str | None:
    if spec.name == "python":
        body = _child_by_field_name(def_node, "body")
        if body is None:
            return None
        for child in _children(body):
            if _kind(child) == "expression_statement":
                grandchildren = _children(child)
                inner = grandchildren[0] if grandchildren else None
                if inner is not None and _kind(inner) == "string":
                    doc = _node_text(inner, raw).strip().strip("'\"")
                    return doc[:500] or None
            break
        return None
    if spec.name in ("typescript", "javascript"):
        return _jsdoc(def_node, raw)
    return None


def _jsdoc(def_node, raw: bytes) -> str | None:
    """The ``/** … */`` block immediately preceding a TS/JS def.

    Walks preceding siblings (skipping decorators) of the def and, when the def
    is wrapped (e.g. ``export_statement``), of its parent.
    """
    candidates = [def_node]
    parent = _parent(def_node)
    if parent is not None and _kind(parent) in ("export_statement",):
        candidates.append(parent)
    for node in candidates:
        prev = _prev_sibling(node)
        while prev is not None and _kind(prev) == "decorator":
            prev = _prev_sibling(prev)
        if prev is not None and _kind(prev) == "comment":
            text = _node_text(prev, raw).strip()
            if text.startswith("/**"):
                inner = text.removeprefix("/**").removesuffix("*/")
                lines = [ln.strip().lstrip("*").strip() for ln in inner.splitlines()]
                cleaned = " ".join(ln for ln in lines if ln)
                return cleaned[:500] or None
    return None


def _decorators(def_node, raw: bytes, spec: LanguageSpec) -> list[str] | None:
    """Decorator strings on a def (``@app.route(...)``, ``@Input()``, …)."""
    out: list[str] = []
    if spec.name == "python":
        parent = _parent(def_node)
        if parent is not None and _kind(parent) == "decorated_definition":
            out = [
                _node_text(c, raw).strip()
                for c in _children(parent)
                if _kind(c) == "decorator"
            ]
    elif spec.name in ("typescript", "javascript"):
        # class decorators are own children; method decorators are prev siblings
        own = [
            _node_text(c, raw).strip()
            for c in _children(def_node)
            if _kind(c) == "decorator"
        ]
        preceding: list[str] = []
        prev = _prev_sibling(def_node)
        while prev is not None and _kind(prev) == "decorator":
            preceding.append(_node_text(prev, raw).strip())
            prev = _prev_sibling(prev)
        out = list(reversed(preceding)) + own
    out = [d[:200] for d in out if d]
    return out or None
