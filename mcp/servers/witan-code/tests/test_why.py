"""`witan code why`: whether a file is in the code graph, and the rule if not.

The rules come from the indexer's own filter (`indexer.why_excluded`), so each
case below is checked against what a full index of the same checkout actually
collects. An explanation that drifted from the walk would be worse than none.
"""

import json
import subprocess
from pathlib import Path

import pytest

from witan_code import cli as cli_module
from witan_code import indexer
from witan_code import output as output_module

from .conftest import requires_stack

REPO = "https://github.com/test/why"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path, monkeypatch) -> Path:
    """A git checkout holding one file per case, cwd'd into."""
    monkeypatch.setenv("WITAN_REPO", REPO)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    src = tmp_path / "repo"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    (src / "kept.py").write_text("def kept():\n    return 1\n")
    (src / "README.md").write_text("# readme\n")
    (src / "node_modules" / "pkg").mkdir(parents=True)
    (src / "node_modules" / "pkg" / "index.js").write_text("var x = 1;\n")
    (src / "vendored" / ".git").mkdir(parents=True)
    (src / "vendored" / "dep.py").write_text("x = 1\n")
    (src / ".gitignore").write_text("out/\n")
    (src / "out").mkdir()
    (src / "out" / "gen.py").write_text("x = 1\n")
    monkeypatch.chdir(src)
    output_module.set_output_format("txt")
    yield src.resolve()
    output_module.set_output_format("txt")


CASES = [
    ("kept.py", None, None),
    ("README.md", indexer.Exclusion.UNSUPPORTED_LANGUAGE, "README.md"),
    ("node_modules/pkg/index.js", indexer.Exclusion.EXCLUDED_PATH, "node_modules"),
    ("vendored/dep.py", indexer.Exclusion.NESTED_CHECKOUT, "vendored"),
    ("out/gen.py", indexer.Exclusion.GITIGNORED, "out/gen.py"),
]


@pytest.mark.parametrize(("rel", "rule", "matched"), CASES)
def test_why_excluded_names_the_rule_and_what_it_matched(checkout, rel, rule, matched):
    found = indexer.why_excluded(checkout / rel, checkout)

    if rule is None:
        assert found is None
    else:
        assert found == (rule, checkout / matched)


def test_why_excluded_agrees_with_what_a_full_index_collects(checkout):
    """The point of sharing the filter: a file is collected exactly when no
    rule excludes it."""
    collected = set(indexer._collect_files(checkout))

    for rel, rule, _ in CASES:
        assert (checkout / rel in collected) == (rule is None), rel


def test_a_tracked_file_a_pattern_matches_is_not_gitignored(checkout):
    """git keeps tracking it, and so does the index."""
    (checkout / ".gitignore").write_text("out/\n*.py\n")
    _git(checkout, "add", "-f", "kept.py")

    assert indexer.why_excluded(checkout / "kept.py", checkout) is None


def test_the_walk_without_git_applies_the_same_path_rules(tmp_path):
    """Outside a work tree `_walk_files` prunes directories with the same
    rules `_filter_listed` applies to git's listing."""
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "b.py").write_text("x = 1\n")
    (tmp_path / "wt").mkdir()
    (tmp_path / "wt" / ".git").write_text("gitdir: /somewhere\n")
    (tmp_path / "wt" / "c.py").write_text("x = 1\n")

    assert indexer._walk_files(tmp_path) == [tmp_path / "a.py"]


def _why(rel: str, capsys, fmt: str = "json") -> dict:
    output_module.set_output_format(fmt)
    cli_module.why(Path(rel))
    return json.loads(capsys.readouterr().out)["rows"][0]


def _fresh_server(monkeypatch):
    from witan_code import config as cfg_module
    from witan_code import server as server_module

    monkeypatch.setattr(server_module, "cfg", cfg_module.load())
    server_module._clients.clear()


def test_a_repo_with_no_code_graph_says_so(checkout, monkeypatch, capsys):
    _fresh_server(monkeypatch)

    row = _why("kept.py", capsys)

    assert row["indexed"] is False
    assert row["store"] == ""
    assert row["state"] == f"not indexed: {REPO} has no code graph"


def test_a_rule_is_reported_even_when_the_repo_has_no_code_graph(
    checkout, monkeypatch, capsys
):
    """Indexing the repo would not help a file a rule excludes."""
    _fresh_server(monkeypatch)

    row = _why("README.md", capsys)

    assert row["state"] == "not indexed: unsupported language"


def test_advice_paths_are_relative_to_the_cwd(checkout, monkeypatch, capsys):
    """The suggested commands run where the user is, which may be below the
    repo root; the reported path stays repo-relative."""
    _fresh_server(monkeypatch)
    sub = checkout / "pkg"
    (sub / "vendored" / ".git").mkdir(parents=True)
    (sub / "vendored" / "dep.py").write_text("x = 1\n")
    (sub / "mod.py").write_text("x = 1\n")
    monkeypatch.chdir(sub)

    nested = _why("vendored/dep.py", capsys)
    unindexed = _why("mod.py", capsys)

    assert nested["path"] == "pkg/vendored/dep.py"
    assert nested["matched"] == "pkg/vendored"
    assert "`witan code index vendored`" in nested["detail"]
    assert unindexed["path"] == "pkg/mod.py"
    assert "`witan code index ..`" in unindexed["detail"]


@requires_stack
@pytest.mark.parametrize(("rel", "rule", "matched"), CASES)
def test_why_reports_each_case_against_the_indexed_graph(
    checkout, monkeypatch, capsys, rel, rule, matched
):
    _fresh_server(monkeypatch)
    indexer.index_path(checkout)

    row = _why(rel, capsys)

    assert row["path"] == rel
    assert row["repo"] == REPO
    assert row["view"] == "main"
    assert row["indexed"] is (rule is None)
    assert row["rule"] == (str(rule) if rule else "")
    assert row["matched"] == (matched if rule else "")
    if rule is None:
        assert row["state"].startswith("indexed (python, at ")


@requires_stack
def test_a_file_not_indexed_yet_points_at_the_index_command(
    checkout, monkeypatch, capsys
):
    _fresh_server(monkeypatch)
    indexer.index_path(checkout)
    (checkout / "new.py").write_text("x = 1\n")

    row = _why("new.py", capsys)

    assert row["indexed"] is False
    assert row["state"] == "not indexed yet"
    assert "`witan code index new.py`" in row["detail"]


@requires_stack
def test_rows_a_rule_now_excludes_are_called_out(checkout, monkeypatch, capsys):
    """A file indexed by name keeps its rows until a full index purges them,
    so the graph can hold a file the rules exclude."""
    _fresh_server(monkeypatch)
    (checkout / "build").mkdir()
    (checkout / "build" / "gen.py").write_text("x = 1\n")
    indexer.index_path(checkout / "build" / "gen.py")

    row = _why("build/gen.py", capsys)

    assert row["indexed"] is True
    assert row["rule"] == "excluded path"
    assert row["state"] == "in the graph, but excluded: excluded path"


@requires_stack
def test_text_output_names_the_rule_and_the_fix(checkout, monkeypatch, capsys):
    _fresh_server(monkeypatch)
    indexer.index_path(checkout)
    output_module.set_output_format("txt")

    cli_module.why(Path("vendored/dep.py"))

    out = " ".join(capsys.readouterr().out.split())
    assert "vendored/dep.py: not indexed: nested checkout" in out
    assert "`witan code index vendored`" in out


def test_a_directory_is_refused(checkout, capsys):
    with pytest.raises(SystemExit):
        cli_module.why(Path("node_modules"))

    assert "is not a file" in capsys.readouterr().err
