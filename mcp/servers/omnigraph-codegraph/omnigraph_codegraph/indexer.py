"""Tree-sitter code indexer for the Layer-2 code graph.

Walks a repo, extracts symbols (functions, methods, classes, modules) and
best-effort relationship edges, and writes them to a per-repo Omnigraph store.

Call/Reference/Import/Inherits resolution is HEURISTIC: identifiers are matched
to known Symbol names within the same repo, preferring same-file definitions,
then imported modules, then any repo-wide match. It is intentionally syntactic
and will miss dynamic dispatch and produce occasional false links.
"""

import hashlib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config as cfg_module
from . import repo as repo_module
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
        extensions=(".ts",),
        grammar="typescript",
        scm="typescript.scm",
        kinds={
            "function": "function",
            "method": "method",
            "class": "class",
            "interface": "interface",
            "type": "type",
        },
    ),
    LanguageSpec(
        name="tsx",
        extensions=(".tsx",),
        grammar="tsx",
        scm="typescript.scm",
        kinds={
            "function": "function",
            "method": "method",
            "class": "class",
            "interface": "interface",
            "type": "type",
        },
    ),
    LanguageSpec(
        name="javascript",
        extensions=(".js", ".jsx", ".mjs", ".cjs"),
        grammar="javascript",
        scm="typescript.scm",
        kinds={"function": "function", "method": "method", "class": "class"},
    ),
)

_EXT_TO_SPEC: dict[str, LanguageSpec] = {
    ext: spec for spec in _LANGUAGES for ext in spec.extensions
}

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


@dataclass
class IndexStats:
    scanned: int = 0
    indexed: int = 0
    skipped: int = 0
    symbols: int = 0
    edges: int = 0
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

    files = _collect_files(target)
    stats = IndexStats()
    for path in files:
        stats.scanned += 1
        try:
            _index_file(path, base, slug, client, force=force, stats=stats)
        except Exception as exc:  # noqa: BLE001 — one bad file must not abort
            stats.errors += 1
            print(f"codegraph: failed to index {path}: {exc}", file=sys.stderr)
    return stats


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


def _index_file(
    path: Path,
    base: Path,
    slug: str,
    client: OmnigraphClient,
    *,
    force: bool,
    stats: IndexStats,
) -> None:
    spec = _EXT_TO_SPEC[path.suffix]
    raw = path.read_bytes()
    content_hash = hashlib.sha256(raw).hexdigest()

    try:
        rel = path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        rel = path.name
    file_id = f"{slug}#{rel}"

    if not force:
        existing = client.read("read.gq", "get_file", {"id": file_id})
        if existing and existing[0].get("content_hash") == content_hash:
            stats.skipped += 1
            return

    parsed = _parse_file(raw, path, spec, slug, file_id, rel, content_hash)
    if parsed is None:
        return

    # Reindex: delete prior data for this file before inserting (separate calls).
    _delete_file_data(file_id, client)

    _insert_file(parsed, slug, client, stats)
    stats.indexed += 1


def _delete_file_data(file_id: str, client: OmnigraphClient) -> None:
    # Deletes must not be mixed with inserts; run as standalone change() calls.
    client.change("delete.gq", "delete_symbols_in_file", {"file_id": file_id})
    client.change("delete.gq", "delete_file", {"id": file_id})


def _insert_file(
    parsed: ParsedFile,
    slug: str,
    client: OmnigraphClient,
    stats: IndexStats,
) -> None:
    now = _now_iso()
    client.change(
        "mutations.gq",
        "insert_file",
        {
            "id": parsed.file_id,
            "repo": slug,
            "path": parsed.path,
            "language": parsed.language,
            "content_hash": parsed.content_hash,
            "indexed_at": now,
        },
    )

    by_qualified: dict[str, ParsedSymbol] = {}
    by_name: dict[str, list[ParsedSymbol]] = {}
    for sym in parsed.symbols:
        client.change(
            "mutations.gq",
            "insert_symbol",
            {
                "id": sym.id,
                "repo": slug,
                "file_id": parsed.file_id,
                "name": sym.name,
                "qualified_name": sym.qualified_name,
                "kind": sym.kind,
                "start_line": sym.start_line,
                "end_line": sym.end_line,
                "signature": sym.signature,
                "docstring": sym.docstring,
                "indexed_at": now,
            },
        )
        stats.symbols += 1
        client.change(
            "mutations.gq", "link_defines", {"from": parsed.file_id, "to": sym.id}
        )
        stats.edges += 1
        by_qualified[sym.qualified_name] = sym
        by_name.setdefault(sym.name, []).append(sym)

    # Contains: lexical nesting within this file.
    for container_qn, child_qn in parsed.contains:
        if container_qn and container_qn in by_qualified and child_qn in by_qualified:
            client.change(
                "mutations.gq",
                "link_contains",
                {
                    "from": by_qualified[container_qn].id,
                    "to": by_qualified[child_qn].id,
                },
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
        client.change(
            "mutations.gq",
            "link_calls",
            {"from": origin.id, "to": target.id},
        )
        client.change(
            "mutations.gq",
            "link_references",
            {"from": origin.id, "to": target.id},
        )
        stats.edges += 2

    # Heuristic Inherits: resolve base class names to same-file class symbols.
    for class_qn, bases in parsed.inherits.items():
        child = by_qualified.get(class_qn)
        if child is None:
            continue
        for base_name in bases:
            target = _resolve_local(base_name, by_name)
            if target is not None and target.id != child.id:
                client.change(
                    "mutations.gq",
                    "link_inherits",
                    {"from": child.id, "to": target.id},
                )
                stats.edges += 1

    # Heuristic Imports: resolve imported names to same-file symbols (best-effort;
    # cross-file resolution is left to query-time lookups by name).
    for iname in parsed.imports:
        target = _resolve_local(iname, by_name)
        if target is not None:
            client.change(
                "mutations.gq",
                "link_imports",
                {"from": parsed.file_id, "to": target.id},
            )
            stats.edges += 1


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
    from tree_sitter_language_pack import get_language

    language = get_language(spec.grammar)
    # Build the parser from the `tree_sitter` package bound to this Language so
    # the produced Nodes are accepted by tree_sitter's Query/QueryCursor. (The
    # language-pack's own get_parser() returns Nodes from a separate binding
    # that QueryCursor.captures() rejects.) This parser wants bytes; _node_text
    # slices into the same bytes.
    from tree_sitter import Parser

    parser = Parser(language)
    tree = parser.parse(raw)
    root = _root(tree)

    parsed = ParsedFile(
        file_id=file_id,
        path=rel,
        language=spec.name,
        content_hash=content_hash,
    )

    # Module-level symbol (the file itself as a module).
    module_name = Path(rel).stem
    module = ParsedSymbol(
        id=f"{file_id}::{module_name}",
        name=module_name,
        qualified_name=module_name,
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

    return parsed


_DEF_NODE_TYPES = {
    "function_definition",
    "class_definition",
    "function_declaration",
    "class_declaration",
    "method_definition",
    "interface_declaration",
    "type_alias_declaration",
    "variable_declarator",
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
    from tree_sitter import Query, QueryCursor

    scm = (_QUERIES_TS_DIR / scm_file).read_text()
    try:
        query = Query(language, scm)
    except Exception:  # noqa: BLE001 — fall back to Language.query
        query = language.query(scm)
    cursor = QueryCursor(query)
    captures = cursor.captures(root)
    out: list[tuple[str, object]] = []
    # captures() returns {capture_name: [nodes]} in recent py-tree-sitter.
    for cap_name, nodes in captures.items():
        for node in nodes:
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
    # First line of the definition, trimmed.
    text = _node_text(def_node, raw).splitlines()
    return text[0].strip() if text else None


def _docstring(def_node, raw: bytes, spec: LanguageSpec) -> str | None:
    if spec.name != "python":
        return None
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


# ── Misc ──────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
