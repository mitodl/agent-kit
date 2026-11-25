"""Resolve calls into other files of the same repo through each file's imports.

The per-file pass in :mod:`witan_code.indexer` links a call only to a symbol
defined in the same file. This module supplies the cross-file half for the
languages whose imports name a file in the repo: Python (absolute and relative
imports) and TypeScript/JavaScript (relative ES module imports).

It is still syntactic. A call resolves when its callee is an imported name, or
an attribute chain rooted at one (``mod.func()``, ``pkg.mod.Cls.method()``).
Calls through an instance (``obj.method()``), re-exports (a package
``__init__.py`` importing a name from a submodule), star imports, ``require()``
and path aliases are not followed. The indexer does record a Python package's
re-exports, explicit and star, as Imports edges of their own
(``indexer._reexport_edges``), from :func:`module_imports` and
:func:`declared_all`.
"""

import posixpath
from collections.abc import Iterable
from dataclasses import dataclass

LANGUAGES = frozenset({"python", "typescript", "javascript"})

_PY_SUFFIXES = (".py", ".pyi")
_JS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
# ESM TypeScript imports name the emitted file (`./b.js`) for a `./b.ts` source.
_JS_TO_TS = {
    ".js": (".ts", ".tsx"),
    ".jsx": (".tsx",),
    ".mjs": (".mts",),
    ".cjs": (".cts",),
}


@dataclass(frozen=True)
class ModuleRef:
    """A module named by an import, as a slash path without a file suffix.

    ``exact`` paths are repo-relative (a relative import, resolved against the
    importing file). An inexact path is a Python absolute import (``a/b`` for
    ``import a.b``), which may sit under any import root in the repo.
    """

    path: str
    exact: bool

    def child(self, segments: Iterable[str]) -> "ModuleRef":
        return ModuleRef("/".join((self.path, *segments)), self.exact)


@dataclass(frozen=True)
class ImportBinding:
    """What a local name is bound to: a module, or a ``member`` of one."""

    module: ModuleRef
    member: str | None = None


def imports(language: str, root, raw: bytes, rel: str) -> dict[str, ImportBinding]:
    """Local name -> binding for every import in the file at ``rel``.

    Imports anywhere in the file count, including ones inside a function body,
    and a later import of a name replaces an earlier one.
    """
    if language == "python":
        return _python_imports(root, raw, rel)
    return _js_imports(root, raw, rel)


def callee_chain(node, raw: bytes) -> tuple[str, ...] | None:
    """The dotted name a call goes through, ending at the captured ``node``.

    ``node`` is the identifier a call capture names: ``f`` in ``f()``, or the
    attribute in ``a.b.f()``, which gives ``("a", "b", "f")``. ``None`` when the
    receiver is not a plain name chain (``self``/``this`` still count as names,
    and simply never match an import).
    """
    parent = node.parent
    if parent is not None and parent.type in ("attribute", "member_expression"):
        receiver = parent.child_by_field_name("object")
        head = _name_chain(receiver, raw)
        return None if head is None else (*head, _text(node, raw))
    return (_text(node, raw),)


def resolve_call(
    chain: tuple[str, ...],
    bindings: dict[str, ImportBinding],
    modules: "ModuleIndex",
    language: str,
    importer: str,
) -> tuple[str, str] | None:
    """``(file, qualified_name)`` the call ``chain`` names, or ``None``.

    The file is a known repo path; whether it defines ``qualified_name`` is for
    the caller to check against the symbols it has.
    """
    binding = bindings.get(chain[0])
    if binding is None:
        return None
    rest = chain[1:]
    module = _submodule(binding, modules, language, importer)
    if module is None:
        if binding.member is not None:
            target = modules.find(language, binding.module, importer)
            if target is None:
                return None
            return target, ".".join((binding.member, *rest))
        module = binding.module
    if not rest:
        return None
    # Python: the longest module prefix of the chain that is a file, leaving at
    # least one segment as the qualified name inside it (`pkg.mod.func()` after
    # `import pkg`). An ES namespace import names exactly one file.
    splits = range(len(rest) - 1, -1, -1) if language == "python" else (0,)
    for split in splits:
        target = modules.find(language, module.child(rest[:split]), importer)
        if target is not None:
            return target, ".".join(rest[split:])
    return None


def external_names(
    chain: tuple[str, ...], binding: ImportBinding, modules: "ModuleIndex"
) -> list[str]:
    """The dotted names ``chain`` uses, when its import names no repo file.

    ``chain`` is a Python name chain rooted at the local name ``binding``
    binds. ``now_in_utc`` after ``from mitol.common.utils.datetime import
    now_in_utc`` gives ``mitol.common.utils.datetime.now_in_utc``;
    ``utils.now_in_utc`` after ``from mitol.common import utils`` gives
    ``mitol.common.utils`` and ``mitol.common.utils.now_in_utc``. Each
    attribute step is a name of its own because nothing here knows where the
    imported object ends: ``Base.objects`` uses ``Base``, and
    ``Cls.method()`` uses ``Cls.method``, and a reader matches a definition
    against one exact name.

    Empty when the import is relative, or any module along the chain is a file
    of this repo: that use is the same-repo pass's to link, which is also what
    keeps a library's uses of its own modules out.
    """
    if binding.module.exact:
        return []
    names = _member_chain(chain, binding)
    for depth in range(len(names) + 1):
        if modules.has_python_module(binding.module.child(names[:depth])):
            return []
    module = tuple(binding.module.path.split("/"))
    return [".".join((*module, *names[:depth])) for depth in range(1, len(names) + 1)]


def dotted_name(chain: tuple[str, ...], binding: ImportBinding) -> str:
    """The whole of ``chain`` as one dotted name under ``binding``'s module,
    the longest of :func:`external_names`, without asking the repo."""
    return ".".join((*binding.module.path.split("/"), *_member_chain(chain, binding)))


def _member_chain(chain: tuple[str, ...], binding: ImportBinding) -> tuple[str, ...]:
    """``chain`` below ``binding``'s module: the member it imports, then the
    attributes taken on it."""
    return chain[1:] if binding.member is None else (binding.member, *chain[1:])


def name_uses(
    root, raw: bytes, names: Iterable[str]
) -> list[tuple[int, tuple[str, ...]]]:
    """``(line, name chain)`` for every load of one of ``names`` in a Python file.

    A load is any appearance of the name outside an import statement other
    than as a name being defined or a keyword argument's name: a call, a base
    class, a decorator, an annotation, a constant passed as a value. The chain
    runs down the attribute accesses on it, so ``models.Base.objects`` gives
    ``("models", "Base", "objects")``. ``line`` is 1-based.

    Syntactic, like the rest of this module: a parameter's own name is not a
    use, but a load of a parameter or local that shadows an imported name
    still counts as one.
    """
    wanted = frozenset(names)
    out: list[tuple[int, tuple[str, ...]]] = []
    if not wanted:
        return out
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in _PY_IMPORT_NODES:
            continue
        if node.type == "identifier":
            text = _text(node, raw)
            if text in wanted and not _defines(node):
                chain = [text]
                cur = node
                while (
                    cur.parent is not None
                    and cur.parent.type == "attribute"
                    and cur.parent.child_by_field_name("object") == cur
                ):
                    cur = cur.parent
                    chain.append(_text(cur.child_by_field_name("attribute"), raw))
                out.append((node.start_point[0] + 1, tuple(chain)))
            continue
        stack.extend(reversed(node.children))
    return out


_PY_IMPORT_NODES = frozenset(
    {"import_statement", "import_from_statement", "future_import_statement"}
)

# (parent node type, field) pairs whose identifier names something rather
# than loading it. The `attribute` field is a member, not a name in scope.
_PY_DEFINING_FIELDS = frozenset(
    {
        ("attribute", "attribute"),
        ("keyword_argument", "name"),
        ("function_definition", "name"),
        ("class_definition", "name"),
        ("default_parameter", "name"),
        ("typed_default_parameter", "name"),
    }
)
# Parents whose bare identifier children are parameter names.
_PY_PARAMETER_LISTS = frozenset({"parameters", "lambda_parameters", "typed_parameter"})


def _defines(node) -> bool:
    parent = node.parent
    if parent is None:
        return False
    if parent.type in _PY_PARAMETER_LISTS:
        return True
    return any(
        parent.type == kind and parent.child_by_field_name(field) == node
        for kind, field in _PY_DEFINING_FIELDS
    )


def _submodule(
    binding: ImportBinding, modules: "ModuleIndex", language: str, importer: str
) -> ModuleRef | None:
    """``pkg/mod`` when ``from pkg import mod`` imports a submodule file."""
    if binding.member is None or language != "python":
        return None
    sub = binding.module.child([binding.member])
    return sub if modules.find(language, sub, importer) is not None else None


class ModuleIndex:
    """Maps a :class:`ModuleRef` to one of a known set of repo paths."""

    def __init__(self, paths: Iterable[str]) -> None:
        self._paths = frozenset(paths)
        self._py_roots: dict[str, list[str]] | None = None

    def find(self, language: str, ref: ModuleRef, importer: str) -> str | None:
        if language == "python":
            if ref.exact:
                return self._first(_py_candidates(ref.path))
            matches = self._python_roots().get(ref.path, [])
            return _nearest(matches, importer)
        return self._first(_js_candidates(ref.path))

    def has_python_module(self, ref: ModuleRef) -> bool:
        """Whether any repo file is the absolute module ``ref``, under any
        import root. Unlike :meth:`find`, two candidates are still a match."""
        return ref.path in self._python_roots()

    def _first(self, candidates: Iterable[str]) -> str | None:
        return next((c for c in candidates if c in self._paths), None)

    def _python_roots(self) -> dict[str, list[str]]:
        """Absolute module path -> files, over every import root in the repo.

        A directory is an import root when it is not itself a package (has no
        ``__init__.py``), so ``pkg/sub/mod.py`` under a root registers as
        ``pkg/sub/mod`` and not as ``sub/mod``, which would let ``import sub``
        or a stdlib name like ``import logging`` match a module deep inside a
        package.

        A ``.pyi`` stub next to its ``.py`` is the same module, so only the
        implementation registers, as relative imports already prefer it.
        Registering both would make every absolute import of it a tie.
        """
        if self._py_roots is None:
            roots: dict[str, list[str]] = {}
            for path in sorted(self._paths):
                stem, suffix = posixpath.splitext(path)
                if suffix not in _PY_SUFFIXES:
                    continue
                if suffix == ".pyi" and f"{stem}.py" in self._paths:
                    continue
                parts = stem.split("/")
                if parts[-1] == "__init__":
                    parts.pop()
                for start in range(len(parts)):
                    if self._is_package(parts[:start]):
                        continue
                    roots.setdefault("/".join(parts[start:]), []).append(path)
            self._py_roots = roots
        return self._py_roots

    def _is_package(self, parts: list[str]) -> bool:
        if not parts:
            return False
        base = "/".join(parts)
        return any(f"{base}/__init__{s}" in self._paths for s in _PY_SUFFIXES)


def _py_candidates(path: str) -> list[str]:
    return [path + s for s in _PY_SUFFIXES] + [
        f"{path}/__init__{s}" for s in _PY_SUFFIXES
    ]


def _js_candidates(path: str) -> list[str]:
    if path == ".":  # the repo root, which only its index file can stand for
        return [f"index{s}" for s in _JS_SUFFIXES]
    stem, suffix = posixpath.splitext(path)
    if suffix in _JS_SUFFIXES:
        return [path, *(stem + s for s in _JS_TO_TS.get(suffix, ()))]
    return [path + s for s in _JS_SUFFIXES] + [f"{path}/index{s}" for s in _JS_SUFFIXES]


def _nearest(matches: list[str], importer: str) -> str | None:
    """The match sharing the longest directory prefix with ``importer``.

    ``None`` on a tie, rather than a guess between two packages of one name.
    """
    if len(matches) < 2:
        return matches[0] if matches else None
    importer_dirs = importer.split("/")[:-1]

    def shared(path: str) -> int:
        n = 0
        for a, b in zip(path.split("/")[:-1], importer_dirs, strict=False):
            if a != b:
                break
            n += 1
        return n

    ranked = sorted(matches, key=shared, reverse=True)
    if shared(ranked[0]) == shared(ranked[1]):
        return None
    return ranked[0]


# ── Python ────────────────────────────────────────────────────────


def _python_imports(root, raw: bytes, rel: str) -> dict[str, ImportBinding]:
    out: dict[str, ImportBinding] = {}
    for node in _walk(root, ("import_statement", "import_from_statement")):
        if node.type == "import_statement":
            for name in node.children_by_field_name("name"):
                if name.type == "aliased_import":
                    dotted = _text(name.child_by_field_name("name"), raw)
                    alias = _text(name.child_by_field_name("alias"), raw)
                    out[alias] = ImportBinding(_absolute(dotted))
                else:
                    # `import a.b.c` binds `a`; the chain walks down from it.
                    top = _text(name, raw).split(".")[0]
                    out[top] = ImportBinding(_absolute(top))
            continue
        module = _from_module(node.child_by_field_name("module_name"), raw, rel)
        if module is None:
            continue
        for name in node.children_by_field_name("name"):
            if name.type == "aliased_import":
                member = _text(name.child_by_field_name("name"), raw)
                local = _text(name.child_by_field_name("alias"), raw)
            else:
                member = local = _text(name, raw)
            out[local] = ImportBinding(module, member)
    return out


@dataclass(frozen=True)
class ModuleImport:
    """One name a module-level ``from`` import binds: ``member`` of
    ``module`` as ``local``, or both ``None`` for ``from module import *``.
    ``line`` is the statement's, 1-based."""

    module: ModuleRef
    member: str | None
    local: str | None
    line: int


def module_imports(root, raw: bytes, rel: str) -> list[ModuleImport]:
    """A Python file's module-level ``from`` imports, in source order.

    Only statements at the top of the module: an import inside a function, or
    under ``if TYPE_CHECKING:``, binds nothing on the module when it is
    imported, so it is no re-export. One under any other ``if`` or a ``try``
    may bind, but which branch runs is not known here, so those are skipped
    too: a re-export missed rather than one claimed.
    """
    out: list[ModuleImport] = []
    for node in root.children:
        if node.type != "import_from_statement":
            continue
        module = _from_module(node.child_by_field_name("module_name"), raw, rel)
        if module is None:
            continue
        line = node.start_point[0] + 1
        if any(c.type == "wildcard_import" for c in node.children):
            out.append(ModuleImport(module, None, None, line))
            continue
        for name in node.children_by_field_name("name"):
            if name.type == "aliased_import":
                member = _text(name.child_by_field_name("name"), raw)
                local = _text(name.child_by_field_name("alias"), raw)
            else:
                member = local = _text(name, raw)
            out.append(ModuleImport(module, member, local, line))
    return out


def declared_all(root, raw: bytes) -> frozenset[str] | None:
    """The names a Python module's ``__all__`` lists, ``None`` without one.

    Only one module-level ``__all__ = [...]`` (or a tuple) of plain string
    literals is read. Any other ``__all__`` statement (a second assignment,
    ``+=``, a computed value) gives the empty set, so a reader that follows a
    star import through this follows nothing it cannot vouch for.
    """
    assignments = []
    for statement in root.children:
        if statement.type != "expression_statement":
            continue
        for node in statement.children:
            if node.type not in ("assignment", "augmented_assignment"):
                continue
            left = node.child_by_field_name("left")
            if left is not None and _text(left, raw) == "__all__":
                assignments.append(node)
    if not assignments:
        return None
    if len(assignments) != 1 or assignments[0].type != "assignment":
        return frozenset()
    value = assignments[0].child_by_field_name("right")
    if value is None or value.type not in ("list", "tuple"):
        return frozenset()
    names = set()
    for item in value.named_children:
        parts = [c for c in item.named_children if c.type == "string_content"]
        if item.type != "string" or len(parts) != 1 or len(item.named_children) != 3:
            return frozenset()  # not a plain literal: an f-string, a name, ...
        names.add(_text(parts[0], raw))
    return frozenset(names)


def _absolute(dotted: str) -> ModuleRef:
    return ModuleRef(dotted.replace(".", "/"), exact=False)


def _from_module(node, raw: bytes, rel: str) -> ModuleRef | None:
    if node is None:
        return None
    if node.type != "relative_import":
        return _absolute(_text(node, raw))
    level = 0
    dotted = ""
    for child in node.children:
        if child.type == "import_prefix":
            level = _text(child, raw).count(".")
        elif child.type == "dotted_name":
            dotted = _text(child, raw)
    package = rel.split("/")[:-1]
    if level - 1 > len(package):
        return None
    parts = package[: len(package) - (level - 1)]
    if dotted:
        parts += dotted.split(".")
    if not parts:
        return None
    return ModuleRef("/".join(parts), exact=True)


# ── TypeScript / JavaScript ───────────────────────────────────────


def _js_imports(root, raw: bytes, rel: str) -> dict[str, ImportBinding]:
    out: dict[str, ImportBinding] = {}
    for node in _walk(root, ("import_statement",)):
        source = node.child_by_field_name("source")
        if source is None:
            continue
        spec = _text(source, raw).strip("'\"`")
        if not spec.startswith("."):
            continue  # a package, or a path alias only the build config knows
        path = posixpath.normpath(posixpath.join(posixpath.dirname(rel), spec))
        if path.startswith(".."):
            continue
        module = ModuleRef(path, exact=True)
        for clause in node.children:
            if clause.type != "import_clause":
                continue
            for part in clause.children:
                if part.type == "namespace_import":
                    ident = next(c for c in part.children if c.type == "identifier")
                    out[_text(ident, raw)] = ImportBinding(module)
                elif part.type == "named_imports":
                    for spec_node in part.children:
                        if spec_node.type != "import_specifier":
                            continue
                        member = _text(spec_node.child_by_field_name("name"), raw)
                        alias = spec_node.child_by_field_name("alias")
                        local = _text(alias, raw) if alias is not None else member
                        out[local] = ImportBinding(module, member)
                # A default import names whatever the module exports as
                # default, which is not a name this can look up.
    return out


# ── tree helpers ──────────────────────────────────────────────────


def _walk(root, types: tuple[str, ...]):
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in types:
            yield node
            continue
        stack.extend(reversed(node.children))


def _name_chain(node, raw: bytes) -> tuple[str, ...] | None:
    if node is None:
        return None
    if node.type in ("identifier", "this"):
        return (_text(node, raw),)
    if node.type in ("attribute", "member_expression"):
        head = _name_chain(node.child_by_field_name("object"), raw)
        tail = node.child_by_field_name(
            "attribute" if node.type == "attribute" else "property"
        )
        if head is None or tail is None:
            return None
        return (*head, _text(tail, raw))
    return None


def _text(node, raw: bytes) -> str:
    return raw[node.start_byte : node.end_byte].decode("utf-8", "replace")
