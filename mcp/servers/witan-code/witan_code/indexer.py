"""Tree-sitter code indexer for the Layer-2 code graph.

Walks a repo, extracts symbols (functions, methods, classes, modules) and
best-effort relationship edges, and writes them to a per-repo Omnigraph store.

Call/Reference/Import/Inherits resolution is HEURISTIC: identifiers are matched
to known Symbol names within the same repo, preferring same-file definitions,
then imported modules, then any repo-wide match. It is intentionally syntactic
and will miss dynamic dispatch and produce occasional false links.
"""

import functools
import hashlib
import importlib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import bridge as bridge_module
from . import bridge_extractors
from . import config as cfg_module
from . import repo as repo_module
from .bridge_extractors import ParsedBinding
from .graph import OmnigraphClient
from .store import ensure_store

# ── Language support ──────────────────────────────────────────────
#
# Adding a language = adding one LanguageSpec: file extensions, the
# tree-sitter grammar name, the .scm query file, and the capture→kind map.

_QUERIES_TS_DIR = Path(__file__).parent / "queries_ts"


@dataclass(frozen=True)
class LanguageSpec:
    name: str
    extensions: tuple[str, ...]
    grammar: str
    scm: str
    # capture prefix "symbol.<kind>" → Symbol kind
    kinds: dict[str, str]


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

_LANGUAGES: tuple[LanguageSpec, ...] = (
    LanguageSpec(
        name="python",
        extensions=(".py", ".pyi"),
        grammar="python",
        scm="python.scm",
        kinds={"function": "function", "class": "class"},
    ),
    LanguageSpec(
        name="typescript",
        extensions=(".ts", ".mts", ".cts", ".tsx"),
        grammar="tsx",
        scm="typescript.scm",
        kinds=_TS_KINDS,
    ),
    LanguageSpec(
        name="javascript",
        extensions=(".js", ".jsx", ".mjs", ".cjs"),
        grammar="tsx",
        scm="typescript.scm",
        kinds=_TS_KINDS,
    ),
    LanguageSpec(
        name="bash",
        extensions=(".sh", ".bash", ".zsh"),
        grammar="bash",
        scm="bash.scm",
        kinds={"function": "function"},
    ),
    LanguageSpec(
        name="yaml",
        extensions=(".yaml", ".yml"),
        grammar="yaml",
        scm="yaml.scm",
        kinds={"key": "key"},
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

    store = ensure_store(slug, cfg)
    client = OmnigraphClient(str(store), cfg.queries_dir)

    # One query for all existing file hashes → the incremental skip check is
    # in-memory, not a query per file.
    existing: dict[str, str] = {}
    if not force:
        for row in client.read("code_read.gq", "all_file_hashes", {}):
            existing[row["slug"]] = row.get("content_hash")

    stats = IndexStats()
    records: list[dict] = []
    reindexed_file_ids: list[str] = []
    bindings: list[ParsedBinding] = []
    touched_files: list[str] = []

    for path in _collect_files(target):
        stats.scanned += 1
        try:
            result = _parse_for_index(path, base, slug, existing, force=force)
        except Exception as exc:  # noqa: BLE001 — one bad file must not abort
            stats.errors += 1
            print(f"codegraph: failed to index {path}: {exc}", file=sys.stderr)
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

    # Drop stale data for changed files (new files have nothing to delete), then
    # bulk-load every node and edge in a single omnigraph call.
    for file_id in reindexed_file_ids:
        _delete_file_data(file_id, client)
    client.load(_dedupe(records), mode="merge")

    # Cross-repo bridge — a SEPARATE phase after the per-repo store write, so the
    # two stores' write locks never nest. A full-repo index (target is the repo
    # root) also runs the repo-level provider extractors and purges by repo;
    # narrower targets only refresh the files they touched.
    full_repo = target.is_dir() and target.resolve() == base.resolve()
    if full_repo:
        bindings.extend(bridge_extractors.extract_repo_bindings(base, slug))
    try:
        stats.bindings = bridge_module.write_bindings(
            bindings,
            slug,
            cfg,
            full_repo=full_repo,
            touched_files=tuple(touched_files),
        )
    except Exception as exc:  # noqa: BLE001 — bridge is best-effort, never fatal
        print(f"codegraph: bridge update failed: {exc}", file=sys.stderr)

    return stats


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


def _collect_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target] if target.suffix in _EXT_TO_SPEC else []

    out: list[Path] = []
    for path in target.rglob("*"):
        if path.is_dir():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in _EXT_TO_SPEC:
            out.append(path)
    return out


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

    try:
        rel = path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        rel = path.name
    file_id = f"{slug}#{rel}"

    if not force and existing.get(file_id) == content_hash:
        return None  # unchanged

    parsed = _parse_file(raw, path, spec, slug, file_id, rel, content_hash)
    if parsed is None:
        return None
    return parsed, (file_id in existing)


def _delete_file_data(file_id: str, client: OmnigraphClient) -> None:
    # Deletes must not be mixed with inserts; run as standalone change() calls.
    client.change("delete.gq", "delete_symbols_in_file", {"file_id": file_id})
    client.change("delete.gq", "delete_file", {"id": file_id})


def _edge(edge_type: str, from_id: str, to_id: str) -> dict:
    return {"edge": edge_type, "from": from_id, "to": to_id}


def _file_records(parsed: ParsedFile, slug: str, stats: IndexStats) -> list[dict]:
    """Build the load() records (node + edge JSONL dicts) for one parsed file."""
    now = _now_iso()
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
    def_capture_nodes = _query_captures(language, spec.scm, root)

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


_DEF_NODE_TYPES = {
    "function_definition",  # python, bash
    "class_definition",
    "function_declaration",
    "generator_function_declaration",
    "class_declaration",
    "method_definition",
    "public_field_definition",  # class arrow methods
    "interface_declaration",
    "type_alias_declaration",
    "enum_declaration",
    "variable_declarator",
    "block_mapping_pair",  # yaml keys (dotted qualified paths)
}


def _walk_defs(
    root,
    raw: bytes,
    spec: LanguageSpec,
    file_id: str,
    module: ParsedSymbol,
    parsed: ParsedFile,
    captures: list[tuple[str, object]],
) -> None:
    # Map each captured definition-name node key → (kind, name_text).
    name_nodes: dict[tuple, tuple[str, str]] = {}
    for cap_name, node in captures:
        if cap_name.startswith("symbol."):
            kind = spec.kinds.get(cap_name.split(".", 1)[1])
            if kind:
                name_nodes[_node_key(node)] = (kind, _node_text(node, raw))

    def enclosing_def(node):
        cur = _parent(node)
        while cur is not None:
            if _kind(cur) in _DEF_NODE_TYPES:
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
        while def_node is not None and _kind(def_node) not in _DEF_NODE_TYPES:
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


def _query_captures(language, scm_file: str, root) -> list[tuple[str, object]]:
    from tree_sitter import Query

    scm = (_QUERIES_TS_DIR / scm_file).read_text()
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
    return out


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


# ── Misc ──────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
