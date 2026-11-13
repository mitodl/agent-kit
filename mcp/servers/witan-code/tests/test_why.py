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


def test_a_symlink_to_a_file_is_explained_under_its_own_name(
    checkout, monkeypatch, capsys
):
    """The walk collects `alias.py` itself, though it points into `build/`."""
    _fresh_server(monkeypatch)
    (checkout / "build").mkdir()
    (checkout / "build" / "real.py").write_text("x = 1\n")
    (checkout / "alias.py").symlink_to(checkout / "build" / "real.py")

    row = _why("alias.py", capsys)

    assert checkout / "alias.py" in indexer._collect_files(checkout)
    assert row["path"] == "alias.py"
    assert row["rule"] == ""


def test_a_file_under_a_symlinked_directory_names_that_rule(
    checkout, tmp_path, monkeypatch, capsys
):
    """Neither the git listing nor the disk walk descends into a symlinked
    directory, so its files are never collected."""
    _fresh_server(monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.py").write_text("x = 1\n")
    (checkout / "linked").symlink_to(elsewhere, target_is_directory=True)

    row = _why("linked/x.py", capsys)

    assert checkout / "linked" / "x.py" not in indexer._collect_files(checkout)
    assert checkout / "linked" / "x.py" not in indexer._walk_files(checkout)
    assert row["path"] == "linked/x.py"
    assert row["rule"] == "symlinked directory"
    assert row["matched"] == "linked"


def test_advice_keeps_an_explicit_repo(checkout, monkeypatch, capsys):
    """A checkout with no remote only indexes with the same `--repo`."""
    _fresh_server(monkeypatch)
    output_module.set_output_format("json")

    cli_module.why(Path("kept.py"), repo="https://github.com/test/no remote")

    row = json.loads(capsys.readouterr().out)["rows"][0]
    assert row["detail"].endswith(
        "`witan code index . --repo 'https://github.com/test/no remote'`."
    )


def _cluster_store(monkeypatch, error: str) -> None:
    """Point the lookup at a cluster graph whose probe fails with ``error``."""
    from witan_code import server as server_module
    from witan_code import store as store_module

    class _Client:
        def list_branches(self):
            raise RuntimeError(error)

    ref = store_module.StoreRef("http://cluster.invalid", "code-why")
    monkeypatch.setattr(
        server_module.store_module, "store_for_repo", lambda *_a, **_k: ref
    )
    monkeypatch.setattr(store_module.StoreRef, "client", lambda *_a, **_k: _Client())


def test_an_unreachable_cluster_graph_is_an_error_not_an_absent_graph(
    checkout, monkeypatch, capsys
):
    _fresh_server(monkeypatch)
    _cluster_store(monkeypatch, "connection refused")
    output_module.set_output_format("json")

    with pytest.raises(SystemExit):
        cli_module.why(Path("kept.py"))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "could not be read: connection refused" in captured.err


def test_a_cluster_graph_that_is_not_served_is_an_absent_graph(
    checkout, monkeypatch, capsys
):
    _fresh_server(monkeypatch)
    _cluster_store(monkeypatch, "graph 'code-why' not found")

    row = _why("kept.py", capsys)

    assert row["state"] == f"not indexed: {REPO} has no code graph"


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


# ── Which graph `why` and `doctor` read ──────────────────────────────────────


@pytest.fixture
def qa_target(checkout, tmp_path, monkeypatch):
    """A `[targets.qa]` whose code_dir differs from the default one, selected
    the way witan's launcher forwards `--target qa`."""
    from witan_code import config as cfg_module
    from witan_code.selected_target import set_selected_target

    config = tmp_path / "config.toml"
    config.write_text(
        f'code_dir = "{(tmp_path / "default-code").as_posix()}"\n'
        "[targets.qa]\n"
        f'code_dir = "{(tmp_path / "qa-code").as_posix()}"\n'
    )
    monkeypatch.setenv("WITAN_CONFIG", str(config))
    monkeypatch.delenv("WITAN_CODE_DIR", raising=False)
    _fresh_server(monkeypatch)
    set_selected_target("qa")
    yield cfg_module.load("qa")
    set_selected_target(None)


@requires_stack
def test_why_reads_the_selected_target(checkout, qa_target, capsys):
    indexer.index_path(checkout, config=qa_target)

    row = _why("kept.py", capsys)

    assert row["indexed"] is True


@requires_stack
def test_doctor_reads_the_selected_target(checkout, qa_target, capsys):
    indexer.index_path(checkout, config=qa_target)
    output_module.set_output_format("json")

    cli_module.doctor()

    rows = json.loads(capsys.readouterr().out)["rows"]
    assert [(r["kind"], r["status"]) for r in rows if r["kind"] == "repo"] == [
        ("repo", "ok")
    ]


def test_why_refuses_when_reads_go_to_a_deployment_but_stores_are_local(
    checkout, monkeypatch, capsys
):
    """`WITAN_REMOTE_URL` sends the `code_*` reads to the deployment, while the
    default `code_transport` leaves the code-graph stores local: the answer
    would describe a graph those reads never look at."""
    _fresh_server(monkeypatch)
    monkeypatch.setenv("WITAN_REMOTE_URL", "https://witan.example.invalid/mcp")
    monkeypatch.setenv("WITAN_OIDC_ISSUER", "https://idp.example.invalid/realms/x")
    output_module.set_output_format("json")

    with pytest.raises(SystemExit) as exc:
        cli_module.why(Path("kept.py"))

    captured = capsys.readouterr()
    assert exc.value.code == 1
    assert captured.out == ""
    message = " ".join(captured.err.split())
    assert "https://witan.example.invalid/mcp" in message
    assert 'Set code_transport = "mcp"' in message
