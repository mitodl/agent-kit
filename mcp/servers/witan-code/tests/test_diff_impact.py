"""``witan code diff-impact``: a branch's diff → symbols → callers, consumers, work."""

from __future__ import annotations

import json
import subprocess

import pytest

from witan_code import cli as cli_module
from witan_code import diff_impact
from witan_code import output as output_module

from .conftest import requires_stack

REPO = "https://github.com/test/cg"
CONSUMER_REPO = "https://github.com/test/infra"

BASE_SVC = """\
import os


class Service:
    def run(self):
        return helper()


def helper():
    return 1


def retired():
    return 2


def main():
    s = Service()
    return s.run()


BASE_URL = os.environ["APP_BASE_URL"]


def legacy():
    return retired()
"""

HEAD_SVC = """\
import os


class Service:
    def run(self):
        return helper()


def helper():
    return 41 + 1


def main():
    s = Service()
    return s.run()


def added():
    return main()


BASE_URL = os.environ["APP_BASE_URL"].rstrip("/")


def legacy():
    return retired()
"""

TEST_SVC = """\
from svc import helper


def test_helper():
    assert helper()
"""

INFRA = """\
config:
  app:env_vars:
    APP_BASE_URL: "https://app.example.com"
"""


# ── diff parsing ──────────────────────────────────────────────────


def test_parse_diff_keeps_each_side_to_the_lines_it_changed():
    text = """\
diff --git a/svc.py b/svc.py
--- a/svc.py
+++ b/svc.py
@@ -10 +10 @@ def helper():
@@ -14,3 +13,0 @@ def retired():
@@ -20,0 +20,3 @@ def main():
diff --git a/new.py b/new.py
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
diff --git a/gone.py b/gone.py
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
diff --git a/img.png b/img.png
Binary files a/img.png and b/img.png differ
"""
    svc, new, gone = diff_impact.parse_diff(text)
    assert (svc.old_path, svc.new_path) == ("svc.py", "svc.py")
    # A pure insertion touches no old line; a pure deletion no new one.
    assert svc.old_lines == {10, 14, 15, 16}
    assert svc.new_lines == {10, 20, 21, 22}
    assert (new.old_path, new.new_path, new.old_lines) == (None, "new.py", set())
    assert (gone.old_path, gone.new_path, gone.new_lines) == ("gone.py", None, set())


def test_parse_diff_reads_headers_only_before_the_first_hunk():
    # -U0 hunks carry no body lines git would emit here, but a SQL comment
    # removed or added in a hunk body starts with `--- ` / `+++ ` all the same.
    text = """\
diff --git a/models/orders.sql b/models/orders.sql
--- a/models/orders.sql
+++ b/models/orders.sql
@@ -1 +1 @@
--- built from staging/stg_orders.sql
+++ built from staging/stg_orders_v2.sql
"""
    (f,) = diff_impact.parse_diff(text)
    assert (f.old_path, f.new_path) == ("models/orders.sql", "models/orders.sql")


def test_parse_diff_keeps_a_pure_rename():
    text = """\
diff --git a/pkg/old_name.py b/pkg/new_name.py
similarity index 100%
rename from pkg/old_name.py
rename to pkg/new_name.py
"""
    (f,) = diff_impact.parse_diff(text)
    assert f.renamed
    assert (f.old_path, f.new_path) == ("pkg/old_name.py", "pkg/new_name.py")
    assert not (f.old_lines or f.new_lines)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_svc.py", True),
        ("pkg/tests/helpers.py", True),
        ("svc_test.go", True),
        ("src/app.test.tsx", True),
        ("conftest.py", True),
        ("src/testing.py", False),
        ("svc.py", False),
    ],
)
def test_is_test_path(path, expected):
    assert diff_impact.is_test_path(path) is expected


def test_changed_symbols_resolves_both_sides_by_qualified_name():
    files = [diff_impact.FileDiff("svc.py", "svc.py")]
    blobs = {("base", "svc.py"): BASE_SVC, ("head", "svc.py"): HEAD_SVC}
    # helper's body (base 10 / head 10), retired (base 13-14), added (head 18-19),
    # and the module-level BASE_URL line (base 21 / head 22).
    files[0].old_lines = {10, 13, 14, 21}
    files[0].new_lines = {10, 18, 19, 22}
    symbols, unparsed = diff_impact.changed_symbols(
        files,
        repo=REPO,
        read_blob=lambda side, path: blobs[(side, path)].encode(),
    )
    assert unparsed == []
    got = {(s.qualified_name, s.status) for s in symbols}
    assert got == {
        ("retired", "removed"),
        ("helper", "modified"),
        ("<module>", "modified"),
        ("added", "added"),
    }
    # Removed first: a caller of one is a break, not a behaviour change.
    assert symbols[0].qualified_name == "retired"
    assert all(s.symbol_id == f"{REPO}#svc.py::{s.qualified_name}" for s in symbols)


DECORATED = """\
import functools


@functools.cache
def cached():
    return 1


class View:
    @property
    def name(self):
        return "v"
"""


@pytest.mark.parametrize(
    ("line", "expected"), [(4, "cached"), (10, "View.name")], ids=["function", "method"]
)
def test_a_decorator_only_edit_maps_to_the_decorated_symbol(line, expected):
    # The parser starts a definition at `def`, after its decorators, so the
    # decorator line alone would otherwise land on <module> or the class.
    files = [diff_impact.FileDiff("d.py", "d.py", {line}, {line})]
    symbols, _ = diff_impact.changed_symbols(
        files, repo=REPO, read_blob=lambda *_: DECORATED.encode()
    )
    assert [s.qualified_name for s in symbols] == [expected]


def test_changed_symbols_reports_files_the_indexer_does_not_parse():
    files = [diff_impact.FileDiff("README.md", "README.md", {1}, {1})]
    symbols, unparsed = diff_impact.changed_symbols(
        files, repo=REPO, read_blob=lambda *_: b""
    )
    assert symbols == []
    assert unparsed == ["README.md"]


# ── the report, against stubbed tools ─────────────────────────────


def _changed(n, kind="function"):
    return [
        diff_impact.ChangedSymbol(
            f"{REPO}#m.py::f{i}", "m.py", f"f{i}", kind, i, "modified"
        )
        for i in range(n)
    ]


def test_report_bounds_every_list_and_says_so():
    many = [{"symbol_id": f"{REPO}#m.py::c{i}", "depth": 1} for i in range(30)]
    tools = {
        "code_indexed_repos": lambda: [{"repo": REPO}],
        "code_symbols_in_file": lambda **_: [{"symbol_id": f"{REPO}#m.py::f0"}],
        "code_impact": lambda **_: {"impacted": many, "truncated": False},
        "code_cross_repo_impact": lambda **_: {"cross_repo": []},
    }
    test_change = diff_impact.ChangedSymbol(
        f"{REPO}#tests/test_m.py::test_f",
        "tests/test_m.py",
        "test_f",
        "function",
        1,
        "modified",
    )
    report = diff_impact.build_report(
        # Methods, whose tests the graph cannot reach, so the name search runs.
        [*_changed(5, kind="method"), test_change],
        [],
        renamed=["old.py -> m.py"],
        repo=REPO,
        branch=None,
        code=tools.__getitem__,
        work=None,
        # Only same-language test files count as a stand-in, never the file itself.
        test_mentions=lambda name: ["m.py", "tests/fixture.yaml", "tests/test_m.py"],
        view=None,
        limit=2,
        max_items=3,
    )
    changed = report["changed_symbols"]
    assert changed["total"] == 5 and len(changed["shown"]) == 2
    callers = changed["shown"][0]["callers"]
    assert callers["total"] == 30 and len(callers["shown"]) == 3
    assert changed["shown"][0]["tests_to_run"]["shown"] == ["tests/test_m.py"]
    # f1 is not in the file's index, so its empty-looking answers are unknowns.
    assert changed["shown"][1]["in_index"] is False
    # A changed test is listed by file, not looked up as a symbol.
    assert report["changed_tests"]["shown"] == ["tests/test_m.py"]
    text = diff_impact.render_text(report, header="h")
    assert "Showing 2 of 5 changed symbols" in text
    assert "callers (30, showing 3)" in text
    assert "m.py::f1 (method, modified) [not in index]" in text
    assert "witan (council) is not installed" in text
    # A rename moves every id in the file, so it must not vanish from the report.
    assert "Renamed files (1): old.py -> m.py" in text
    assert "A renamed file moves every symbol id" in text


def test_a_functions_tests_come_from_its_callers_not_a_name_search():
    """Python calls resolve across files, so a test calling the function is
    one of its callers, and naming it without calling it is not a test of it."""

    def no_search(name):
        raise AssertionError(f"searched for {name!r} by name")

    tools = {
        "code_indexed_repos": lambda: [{"repo": REPO}],
        "code_symbols_in_file": lambda **_: [{"symbol_id": f"{REPO}#m.py::f0"}],
        "code_impact": lambda **_: {
            "impacted": [
                {"symbol_id": f"{REPO}#tests/test_m.py::test_f0", "depth": 1},
                {"symbol_id": f"{REPO}#app.py::serve", "depth": 1},
            ],
            "truncated": False,
        },
        "code_cross_repo_impact": lambda **_: {"cross_repo": []},
    }
    report = diff_impact.build_report(
        _changed(1),
        [],
        renamed=[],
        repo=REPO,
        branch=None,
        code=tools.__getitem__,
        work=None,
        test_mentions=no_search,
        view=None,
        limit=10,
        max_items=10,
    )
    row = report["changed_symbols"]["shown"][0]
    assert row["tests_to_run"]["shown"] == ["tests/test_m.py"]
    assert row["callers"]["shown"] == ["app.py::serve"]


LINKED_PY = """\
class Service:
    def run(self):
        return 1


def helper():
    return 1
"""

LINKED_TS = """\
export class Store {
  get() {
    return 1;
  }
}

export interface Shape {
  size: number;
}

export type Id = string;

export function load() {
  return new Store();
}

export const make = () => 1;
"""


@pytest.mark.parametrize(
    ("path", "line", "qualified_name", "expected"),
    [
        ("svc.py", 7, "helper", True),
        ("svc.py", 1, "Service", True),
        # Recorded as a function, but called through an instance.
        ("svc.py", 3, "Service.run", False),
        ("web/s.ts", 14, "load", True),
        ("web/s.ts", 17, "make", True),
        # `new Store()` and type positions are not calls the parser captures.
        ("web/s.ts", 1, "Store", False),
        ("web/s.ts", 3, "Store.get", False),
        ("web/s.ts", 8, "Shape", False),
        ("web/s.ts", 11, "Id", False),
    ],
)
def test_graph_reaches_callers_only_for_what_the_resolver_links(
    path, line, qualified_name, expected
):
    source = {"svc.py": LINKED_PY, "web/s.ts": LINKED_TS}[path]
    files = [diff_impact.FileDiff(path, path, {line}, {line})]
    (sym,), _ = diff_impact.changed_symbols(
        files, repo=REPO, read_blob=lambda *_: source.encode()
    )
    assert sym.qualified_name == qualified_name
    assert diff_impact.graph_reaches_callers(sym) is expected


def test_test_mentions_match_across_one_language_family():
    tools = {
        "code_indexed_repos": list,
    }
    hook = diff_impact.ChangedSymbol(
        f"{REPO}#src/useCart.ts::useCart",
        "src/useCart.ts",
        "useCart",
        "function",
        1,
        "modified",
    )
    report = diff_impact.build_report(
        [hook],
        [],
        renamed=[],
        repo=REPO,
        branch=None,
        code=tools.__getitem__,
        work=None,
        test_mentions=lambda name: ["src/useCart.test.tsx", "tests/test_cart.py"],
        view=None,
        limit=10,
        max_items=10,
    )
    row = report["changed_symbols"]["shown"][0]
    assert row["tests_to_run"]["shown"] == ["src/useCart.test.tsx"]


def test_unindexed_repo_does_not_read_as_no_impact():
    def unreachable(**_):
        raise AssertionError("no caller lookup against an unindexed repo")

    tools = {
        "code_indexed_repos": lambda: [{"repo": "https://github.com/test/other"}],
        "code_impact": unreachable,
        "code_cross_repo_impact": unreachable,
    }
    report = diff_impact.build_report(
        _changed(1),
        [],
        renamed=[],
        repo=REPO,
        branch=None,
        code=tools.__getitem__,
        work=None,
        test_mentions=lambda name: [],
        view=None,
        limit=10,
        max_items=10,
    )
    assert report["indexed"] is False
    assert "callers" not in report["changed_symbols"]["shown"][0]
    text = diff_impact.render_text(report, header="h")
    assert f"repo: {REPO} (NOT INDEXED)" in text
    assert "callers and cross-repo consumers were not looked up" in text


def test_a_removed_symbol_the_index_lacks_is_flagged():
    # Added after the last indexing run, then removed by this branch: even the
    # base's view lacks it, so its empty impact is "not seen" and carries the
    # same flag as any other symbol.
    removed = diff_impact.ChangedSymbol(
        f"{REPO}#old.py::gone", "old.py", "gone", "function", 1, "removed"
    )
    asked: list[tuple[str, str, dict]] = []

    def recorder(name, result):
        def call(**kwargs):
            asked.append((name, kwargs.get("path", ""), kwargs))
            return result

        return call

    tools = {
        "code_indexed_repos": lambda: [{"repo": REPO}],
        "code_symbols_in_file": recorder("code_symbols_in_file", []),
        "code_impact": recorder("code_impact", {"impacted": [], "truncated": False}),
        "code_cross_repo_impact": recorder(
            "code_cross_repo_impact", {"cross_repo": []}
        ),
    }
    report = diff_impact.build_report(
        [removed],
        [],
        renamed=[],
        repo=REPO,
        branch=None,
        code=tools.__getitem__,
        work=None,
        test_mentions=lambda name: [],
        view="main",
        limit=10,
        max_items=10,
    )
    assert [path for name, path, _ in asked if name == "code_symbols_in_file"] == [
        "old.py"
    ]
    # Every code lookup is pinned to the base's view.
    assert {kwargs["branch"] for _, _, kwargs in asked} == {"main"}
    assert report["changed_symbols"]["shown"][0]["in_index"] is False
    text = diff_impact.render_text(report, header="h")
    assert "come from the base branch's view (main)" in text


def test_a_git_failure_keeps_stdout_for_the_document(tmp_path, monkeypatch, capsys):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_module, "_current_repo", lambda repo: REPO)
    output_module.set_output_format("json")
    try:
        with pytest.raises(SystemExit):
            cli_module.diff_impact(base="no-such-ref", head="HEAD")
    finally:
        output_module.set_output_format("txt")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "git merge-base no-such-ref HEAD failed" in captured.err


# ── end to end, on a fixture graph ────────────────────────────────


def _git(cwd, *args):
    return subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def fixture_graph(tmp_path, monkeypatch):
    """Two indexed repos: ``cg`` on main, ``infra`` providing its env var.

    ``cg`` then gets a ``feature`` branch carrying HEAD_SVC, left unindexed:
    the command must not depend on a branch view existing.
    """
    from witan_code import config as cfg_mod
    from witan_code import indexer
    from witan_code import server as srv

    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()

    infra = tmp_path / "infra"
    infra.mkdir()
    (infra / "Pulumi.QA.yaml").write_text(INFRA)
    indexer.index_path(infra, repo_override=CONSUMER_REPO, config=srv.cfg)

    cg = tmp_path / "cg"
    (cg / "tests").mkdir(parents=True)
    (cg / "svc.py").write_text(BASE_SVC)
    (cg / "tests" / "test_svc.py").write_text(TEST_SVC)
    (cg / "README.md").write_text("cg\n")
    _git(cg, "init", "-q", "-b", "main")
    _git(cg, "add", ".")
    _git(cg, "commit", "-q", "-m", "base")
    indexer.index_path(cg, repo_override=REPO, config=srv.cfg)

    _git(cg, "checkout", "-q", "-b", "feature")
    (cg / "svc.py").write_text(HEAD_SVC)
    (cg / "README.md").write_text("cg, changed\n")
    (cg / "tests" / "test_svc.py").write_text(TEST_SVC + "    assert helper() == 42\n")
    _git(cg, "commit", "-q", "-am", "feature")

    monkeypatch.setenv("WITAN_REPO", REPO)
    monkeypatch.chdir(cg)
    monkeypatch.setattr(cli_module, "_server", srv)
    output_module.set_output_format("json")
    yield cg
    output_module.set_output_format("txt")


@requires_stack
def test_diff_impact_on_a_fixture_graph(fixture_graph, monkeypatch, capsys):
    helper_id = f"{REPO}#svc.py::helper"
    helper_tasks = [{"slug": "tk-fix-helper", "title": "Fix helper", "status": "open"}]
    helper_memories = [{"slug": "les-helper-is-hot", "title": "helper is hot"}]

    def recall(symbol_id, hops, include_superseded, limit):
        # Exactly the seed set, as the removed symbol_context returned it.
        assert (hops, include_superseded) == (0, True)
        found = symbol_id == helper_id
        return {
            "memories": helper_memories if found else [],
            "contradictions": [],
            "seeds": {},
            "symbol": {
                "symbol_id": symbol_id,
                "tasks": helper_tasks if found else [],
            },
        }

    # Only tools witan still has: a removed one is a KeyError, as it is an
    # unknown tool against the real server.
    work = {
        "recall": recall,
        "task_for_branch": lambda branch, repo: [
            {
                "slug": "tk-feature",
                "title": f"{branch} in {repo}",
                "status": "in_progress",
            }
        ],
    }
    monkeypatch.setattr(cli_module, "_work_tool", lambda: work.__getitem__)

    cli_module.diff_impact(base="main", head="feature")
    report = json.loads(capsys.readouterr().out)

    assert report["indexed"] is True
    assert report["branch"] == "feature"
    rows = {r["qualified_name"]: r for r in report["changed_symbols"]["shown"]}
    assert {n: r["status"] for n, r in rows.items()} == {
        "retired": "removed",
        "helper": "modified",
        "<module>": "modified",
        "added": "added",
    }

    helper = rows["helper"]
    assert helper["in_index"] is True
    assert "svc.py::Service.run" in helper["callers"]["shown"]
    # The test file imports and calls helper, so it is one of helper's callers.
    assert helper["tests_to_run"]["shown"] == ["tests/test_svc.py"]
    assert helper["tasks"]["shown"][0]["slug"] == "tk-fix-helper"
    assert helper["memories"]["shown"][0]["slug"] == "les-helper-is-hot"

    # The module-level env read joins to the repo that provides it.
    module = rows["<module>"]
    assert {(b["repo"], b["key"]) for b in module["cross_repo"]["shown"]} == {
        (CONSUMER_REPO, "APP_BASE_URL")
    }

    # Added on the unindexed branch: flagged, so "no callers" is not read as safe.
    assert rows["added"]["in_index"] is False

    assert report["branch_tasks"]["shown"][0]["slug"] == "tk-feature"
    assert report["changed_tests"]["shown"] == ["tests/test_svc.py"]
    assert report["unparsed_files"]["shown"] == ["README.md"]


@requires_stack
def test_diff_impact_without_witan_says_what_it_skipped(
    fixture_graph, monkeypatch, capsys
):
    monkeypatch.setattr(cli_module, "_work_tool", lambda: None)
    output_module.set_output_format("txt")
    # From a subdirectory: paths and the test search still cover the whole repo.
    monkeypatch.chdir(fixture_graph / "tests")

    cli_module.diff_impact(base="main", head="feature")
    out = capsys.readouterr().out

    assert "svc.py::helper (function, modified)" in out
    assert "callers (2): svc.py::Service.run, svc.py::main" in out
    assert "tests to run (1): tests/test_svc.py" in out
    assert "tasks (" not in out
    assert "witan (council) is not installed" in out


@requires_stack
def test_a_removal_resolves_against_the_base_view(fixture_graph, monkeypatch, capsys):
    """Locally, the code tools follow the checkout's branch view once it is
    indexed, and that view no longer has the removed symbol. diff-impact pins
    its lookups to the base's view, where the symbol and its callers exist."""
    from witan_code import indexer
    from witan_code import server as srv

    indexer.index_path(fixture_graph, repo_override=REPO, config=srv.cfg)
    srv._clients.clear()
    srv._store_branches.clear()
    monkeypatch.setattr(cli_module, "_work_tool", lambda: None)

    cli_module.diff_impact(base="main", head="feature")
    rows = {
        r["qualified_name"]: r
        for r in json.loads(capsys.readouterr().out)["changed_symbols"]["shown"]
    }
    assert rows["retired"]["status"] == "removed"
    assert rows["retired"]["in_index"] is True
    assert rows["retired"]["callers"]["shown"] == ["svc.py::legacy"]
    # Symbols the base never had are still flagged: `added` exists only in the
    # branch view, which these lookups no longer read.
    assert rows["added"]["in_index"] is False
