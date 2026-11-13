"""Calls resolved across files through imports (witan_code.xref + the indexer)."""

import subprocess

import pytest

from witan_code import xref

from .conftest import requires_stack, tool_fn

REPO = "https://github.com/test/xref"


def _resolve(files, importer, source, call, language="python"):
    from tree_sitter import Parser

    from witan_code import indexer

    raw = source.encode()
    spec = indexer._EXT_TO_SPEC["." + importer.rsplit(".", 1)[1]]
    root = Parser(indexer._ts_language(spec.grammar)).parse(raw).root_node
    bindings = xref.imports(language, root, raw, importer)
    return xref.resolve_call(
        tuple(call.split(".")), bindings, xref.ModuleIndex(files), language, importer
    )


# ── Resolution, no store ──────────────────────────────────────────

PY_FILES = {
    "src/pkg/__init__.py",
    "src/pkg/b.py",
    "src/pkg/sub/__init__.py",
    "src/pkg/sub/c.py",
    "src/pkg/logging.py",
}


@pytest.mark.parametrize(
    ("source", "call", "expected"),
    [
        ("from pkg.b import f\n", "f", ("src/pkg/b.py", "f")),
        ("from pkg.b import f as g\n", "g", ("src/pkg/b.py", "f")),
        ("from pkg.b import K\n", "K.m", ("src/pkg/b.py", "K.m")),
        ("import pkg.b as bb\n", "bb.f", ("src/pkg/b.py", "f")),
        ("import pkg.sub.c\n", "pkg.sub.c.f", ("src/pkg/sub/c.py", "f")),
        ("from pkg import b\n", "b.f", ("src/pkg/b.py", "f")),
        ("from pkg import name\n", "name", ("src/pkg/__init__.py", "name")),
        ("from .b import f\n", "f", ("src/pkg/b.py", "f")),
        ("from . import b\n", "b.K.m", ("src/pkg/b.py", "K.m")),
        ("from .sub.c import f\n", "f", ("src/pkg/sub/c.py", "f")),
    ],
)
def test_python_calls_resolve_through_imports(source, call, expected):
    assert _resolve(PY_FILES, "src/pkg/a.py", source, call) == expected


def test_a_relative_import_climbs_packages():
    got = _resolve(PY_FILES, "src/pkg/sub/c.py", "from ..b import f\n", "f")
    assert got == ("src/pkg/b.py", "f")


def test_a_module_inside_a_package_is_not_a_top_level_import():
    """`import logging` is the stdlib, not `src/pkg/logging.py`: that file is
    only importable as `pkg.logging`, since `src/pkg` is a package."""
    assert _resolve(PY_FILES, "src/pkg/a.py", "import logging\n", "logging.f") is None


def test_unimported_and_instance_calls_do_not_resolve():
    files, importer = PY_FILES, "src/pkg/a.py"
    assert _resolve(files, importer, "from pkg.b import f\n", "g") is None
    assert _resolve(files, importer, "from pkg.b import f\n", "self.f") is None


def test_two_equally_near_modules_of_one_name_resolve_to_neither():
    files = {"one/util.py", "two/util.py"}
    assert _resolve(files, "main.py", "from util import f\n", "f") is None
    # A nearer one wins over a farther one.
    assert _resolve(files, "one/x.py", "from util import f\n", "f") == (
        "one/util.py",
        "f",
    )


@pytest.mark.parametrize("source", ["from pkg.b import f\n", "from .b import f\n"])
def test_a_sibling_stub_does_not_hide_its_implementation(source):
    """`b.pyi` next to `b.py` is the same module, not a second candidate."""
    files = PY_FILES | {"src/pkg/b.pyi", "src/pkg/__init__.pyi"}
    assert _resolve(files, "src/pkg/a.py", source, "f") == ("src/pkg/b.py", "f")
    assert _resolve(files, "main.py", "from pkg import name\n", "name") == (
        "src/pkg/__init__.py",
        "name",
    )


def test_a_stub_alone_is_the_module():
    files = {"src/pkg/__init__.py", "src/pkg/b.pyi"}
    got = _resolve(files, "main.py", "from pkg.b import f\n", "f")
    assert got == ("src/pkg/b.pyi", "f")


@pytest.mark.parametrize(
    ("source", "call", "expected"),
    [
        ("import { foo } from './b';\n", "foo", ("web/b.ts", "foo")),
        ("import { foo as bar } from './b';\n", "bar", ("web/b.ts", "foo")),
        ("import * as ns from './b';\n", "ns.foo", ("web/b.ts", "foo")),
        ("import { util } from '../lib';\n", "util", ("lib/index.ts", "util")),
        ("import { foo } from './b.js';\n", "foo", ("web/b.ts", "foo")),
        ("import { foo } from 'b';\n", "foo", None),
        ("import Foo from './b';\n", "Foo", None),
    ],
)
def test_ts_calls_resolve_through_relative_imports(source, call, expected):
    files = {"web/a.ts", "web/b.ts", "lib/index.ts"}
    got = _resolve(files, "web/a.ts", source, call, language="typescript")
    assert got == expected


# ── Through the indexer and code_callers ──────────────────────────

B_PY = """\
def target():
    return 1


class Thing:
    def build(self):
        return target()
"""

A_PY = """\
from pkg import b
from pkg.b import Thing, target


def direct():
    return target()


def through_module():
    return b.target()


def through_class():
    return Thing.build(None)
"""


def _git(base, *args):
    subprocess.run(
        ["git", "-C", str(base), *args], check=True, capture_output=True, text=True
    )


@pytest.fixture
def two_modules(tmp_path, monkeypatch):
    """A git repo whose `pkg/a.py` calls into `pkg/b.py`, indexed on main."""
    from witan_code import config as cfg_mod
    from witan_code import server as srv

    monkeypatch.setenv("WITAN_REPO", REPO)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())

    base = tmp_path / "r"
    (base / "pkg").mkdir(parents=True)
    _git(base, "init", "-q", "-b", "main")
    _git(base, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "init")  # fmt: skip
    (base / "pkg" / "__init__.py").write_text("")
    (base / "pkg" / "b.py").write_text(B_PY)
    (base / "pkg" / "a.py").write_text(A_PY)
    monkeypatch.chdir(base)
    _index(base)
    return base


def _index(target):
    from witan_code import indexer
    from witan_code import server as srv

    stats = indexer.index_path(target, config=srv.cfg)
    assert stats.errors == 0
    return stats


def _callers(symbol, direction="callers"):
    from witan_code import server as srv

    srv._clients.clear()
    srv._store_branches.clear()
    srv._git_context.clear()
    rows = tool_fn(srv.code_callers)(f"{REPO}#{symbol}", direction=direction)
    return {f"{r['symbol_id'].split('#', 1)[1]}" for r in rows}


@requires_stack
def test_a_call_into_another_module_is_a_caller(two_modules):
    assert _callers("pkg/b.py::target") == {
        "pkg/a.py::direct",
        "pkg/a.py::through_module",
        "pkg/b.py::Thing.build",
    }
    assert _callers("pkg/b.py::Thing.build") == {"pkg/a.py::through_class"}
    assert _callers("pkg/a.py::direct", direction="callees") == {"pkg/b.py::target"}


@requires_stack
def test_impact_walks_across_files(two_modules):
    from witan_code import server as srv

    impact = tool_fn(srv.code_impact)(f"{REPO}#pkg/b.py::target", max_depth=2)
    reached = {c["symbol_id"].split("#", 1)[1] for c in impact["impacted"]}
    assert "pkg/a.py::through_class" in reached  # via Thing.build


@requires_stack
def test_reindexing_the_callee_alone_keeps_its_callers(two_modules):
    """Re-indexing a file deletes its symbols and every edge into them; the
    run re-links the files that import it, as the edit hook's one-file run."""
    (two_modules / "pkg" / "b.py").write_text(B_PY.replace("return 1", "return 2"))
    _index(two_modules / "pkg" / "b.py")
    assert "pkg/a.py::direct" in _callers("pkg/b.py::target")


@requires_stack
def test_a_callee_defined_after_its_caller_is_linked(two_modules):
    """An agent often writes the call first: the edge can only land when the
    callee's file is indexed, which the import of it makes possible."""
    a = two_modules / "pkg" / "a.py"
    a.write_text(A_PY + "\n\ndef later():\n    return b.fresh()\n")
    _index(a)
    (two_modules / "pkg" / "b.py").write_text(B_PY + "\n\ndef fresh():\n    return 0\n")
    _index(two_modules / "pkg" / "b.py")
    assert _callers("pkg/b.py::fresh") == {"pkg/a.py::later"}


@requires_stack
def test_a_dotted_import_keeps_its_caller_through_a_callee_reindex(two_modules):
    """`import pkg.b` binds `pkg`, so nothing about the import names b.py
    except the module word the relink searches for."""
    (two_modules / "d.py").write_text(
        "import pkg.b\n\n\ndef dotted():\n    return pkg.b.target()\n"
    )
    _index(two_modules / "d.py")
    assert "d.py::dotted" in _callers("pkg/b.py::target")
    (two_modules / "pkg" / "b.py").write_text(B_PY.replace("return 1", "return 3"))
    _index(two_modules / "pkg" / "b.py")
    assert "d.py::dotted" in _callers("pkg/b.py::target")


@requires_stack
def test_a_package_entrypoint_keeps_its_callers_through_a_reindex(two_modules):
    """`from . import target` reaches `pkg/__init__.py` without naming `pkg`."""
    init = two_modules / "pkg" / "__init__.py"
    init.write_text("def target():\n    return 1\n")
    (two_modules / "pkg" / "g.py").write_text(
        "from . import target\n\n\ndef via_init():\n    return target()\n"
    )
    _index(two_modules)
    assert _callers("pkg/__init__.py::target") == {"pkg/g.py::via_init"}
    init.write_text("def target():\n    return 2\n")
    _index(init)
    assert _callers("pkg/__init__.py::target") == {"pkg/g.py::via_init"}


@requires_stack
def test_a_ts_index_keeps_its_callers_through_a_reindex(tmp_path, monkeypatch):
    """`'./index'` and `'.'` reach `lib/index.ts` without naming `lib`."""
    from witan_code import config as cfg_mod
    from witan_code import server as srv

    monkeypatch.setenv("WITAN_REPO", REPO)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    base = tmp_path / "r"
    (base / "lib").mkdir(parents=True)
    _git(base, "init", "-q", "-b", "main")
    index = base / "lib" / "index.ts"
    index.write_text("export function target() {\n  return 1;\n}\n")
    (base / "lib" / "a.ts").write_text(
        "import { target } from './index';\n\n"
        "export function viaIndex() {\n  return target();\n}\n"
    )
    (base / "lib" / "c.ts").write_text(
        "import * as here from '.';\n\n"
        "export function viaDot() {\n  return here.target();\n}\n"
    )
    monkeypatch.chdir(base)
    _index(base)
    expected = {"lib/a.ts::viaIndex", "lib/c.ts::viaDot"}
    assert _callers("lib/index.ts::target") == expected
    index.write_text("export function target() {\n  return 2;\n}\n")
    _index(index)
    assert _callers("lib/index.ts::target") == expected


@requires_stack
def test_a_caller_written_before_its_module_is_linked(two_modules):
    (two_modules / "e.py").write_text(
        "from pkg.new import made\n\n\ndef early():\n    return made()\n"
    )
    _index(two_modules / "e.py")
    (two_modules / "pkg" / "new.py").write_text("def made():\n    return 0\n")
    _index(two_modules / "pkg" / "new.py")
    assert _callers("pkg/new.py::made") == {"e.py::early"}


@requires_stack
def test_a_method_of_the_same_name_does_not_shadow_an_import(two_modules):
    (two_modules / "f.py").write_text(
        "from pkg.b import target\n\n\nclass Store:\n    def target(self):\n"
        "        return 0\n\n\ndef bare():\n    return target()\n"
    )
    _index(two_modules / "f.py")
    assert "f.py::bare" in _callers("pkg/b.py::target")


@requires_stack
def test_removing_a_called_function_does_not_fail_the_index(two_modules):
    (two_modules / "pkg" / "b.py").write_text("class Thing:\n    pass\n")
    _index(two_modules / "pkg" / "b.py")
    assert _callers("pkg/a.py::direct", direction="callees") == set()


@requires_stack
def test_a_branch_views_cross_file_callers_stay_on_the_branch(two_modules):
    _git(two_modules, "checkout", "-q", "-b", "feature/x")
    (two_modules / "pkg" / "c.py").write_text(
        "from .b import target\n\n\ndef added():\n    return target()\n"
    )
    _index(two_modules)
    assert "pkg/c.py::added" in _callers("pkg/b.py::target")

    _git(two_modules, "checkout", "-q", "main")
    on_main = _callers("pkg/b.py::target")
    assert "pkg/c.py::added" not in on_main
    assert "pkg/a.py::direct" in on_main


@requires_stack
def test_a_ts_call_into_another_module_is_a_caller(tmp_path, monkeypatch):
    from witan_code import config as cfg_mod
    from witan_code import server as srv

    monkeypatch.setenv("WITAN_REPO", REPO)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    base = tmp_path / "r"
    base.mkdir()
    _git(base, "init", "-q", "-b", "main")
    (base / "b.ts").write_text("export function foo() {\n  return 1;\n}\n")
    (base / "a.ts").write_text(
        "import { foo } from './b';\nimport * as b from './b';\n\n"
        "export function one() {\n  return foo();\n}\n\n"
        "export function two() {\n  return b.foo();\n}\n"
    )
    monkeypatch.chdir(base)
    _index(base)
    assert _callers("b.ts::foo") == {"a.ts::one", "a.ts::two"}
