"""Tests for branch-aware indexing: git branch → omnigraph branch mapping."""

import subprocess

from witan_code import repo as repo_module

from .conftest import SAMPLE, requires_stack

REPO = "https://github.com/test/cg"


def _git(base, *args):
    subprocess.run(
        ["git", "-C", str(base), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _git_repo(path, branch="main"):
    path.mkdir(exist_ok=True)
    _git(path, "init", "-q", "-b", branch)
    _git(
        path,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "--allow-empty",
        "-q",
        "-m",
        "init",
    )
    return path


# ── sanitize_branch ───────────────────────────────────────────────


def test_sanitize_branch_replaces_unsafe_chars():
    assert repo_module.sanitize_branch("feature/foo bar") == "feature_foo_bar"
    assert repo_module.sanitize_branch("release-1.2.3") == "release-1.2.3"


def test_sanitize_branch_empty_falls_back_to_detached():
    assert repo_module.sanitize_branch("///") == repo_module.DETACHED_BRANCH


# ── store_branch ──────────────────────────────────────────────────


def test_store_branch_default_branch_maps_to_main(tmp_path):
    base = _git_repo(tmp_path / "r")
    assert repo_module.store_branch(base) is None


def test_store_branch_feature_branch_maps_to_sanitized_name(tmp_path):
    base = _git_repo(tmp_path / "r")
    _git(base, "checkout", "-q", "-b", "feature/thing")
    assert repo_module.store_branch(base) == "feature_thing"


def test_nondefault_branch_named_main_maps_to_reserved_name(tmp_path):
    """A feature branch literally named 'main' in a master-default repo must
    not collide with omnigraph's reserved main branch."""
    base = _git_repo(tmp_path / "r", branch="master")
    _git(base, "update-ref", "refs/remotes/origin/master", "HEAD")
    _git(base, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master")
    _git(base, "checkout", "-q", "-b", "main")
    assert repo_module.store_branch(base) == "_main"


def test_no_origin_both_defaults_is_collision_free(tmp_path):
    """Without origin HEAD and with both main and master present, main wins
    the default slot deterministically and master gets its own store branch."""
    base = _git_repo(tmp_path / "r", branch="master")
    _git(base, "branch", "main")
    assert repo_module.store_branch(base) == "master"
    _git(base, "checkout", "-q", "main")
    assert repo_module.store_branch(base) is None


def test_local_branches_protects_reserved_main_mapping(tmp_path):
    base = _git_repo(tmp_path / "r", branch="master")
    _git(base, "branch", "main")
    branches = repo_module.local_branches(base)
    assert "_main" in branches, "prune must see the store name for a git 'main' branch"


def test_branch_store_name_reserves_main():
    assert repo_module.branch_store_name("main") == "_main"
    assert repo_module.branch_store_name("feature/x") == "feature_x"


def test_store_branch_detached_head_maps_to_scratch(tmp_path):
    base = _git_repo(tmp_path / "r")
    _git(base, "checkout", "-q", "--detach")
    assert repo_module.store_branch(base) == repo_module.DETACHED_BRANCH


def test_store_branch_outside_git_is_none(tmp_path):
    assert repo_module.store_branch(tmp_path) is None


def test_local_branches_use_store_names(tmp_path):
    base = _git_repo(tmp_path / "r")
    _git(base, "branch", "feature/x")
    # "main" maps through branch_store_name like store_branch does, so prune
    # compares like with like; the store's own "main" is never pruned anyway.
    assert repo_module.local_branches(base) == {"_main", "feature_x"}


# ── Integration: branch indexes land on omnigraph branches ───────


@requires_stack
def test_feature_branch_indexes_to_own_branch(tmp_path, monkeypatch):
    from witan_code import config as cfg_module
    from witan_code import indexer
    from witan_code.graph import OmnigraphClient

    monkeypatch.setenv("WITAN_REPO", REPO)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    cfg = cfg_module.load()

    base = _git_repo(tmp_path / "r")
    (base / "svc.py").write_text(SAMPLE)
    indexer.index_path(base, config=cfg)

    _git(base, "checkout", "-q", "-b", "feature/new-api")
    (base / "extra.py").write_text("def branch_only_symbol():\n    return 2\n")
    stats = indexer.index_path(base, config=cfg)
    assert stats.indexed >= 1

    store = str(cfg_module.store_path(REPO, cfg.code_dir))
    main_client = OmnigraphClient(store, cfg.queries_dir)
    assert "feature_new-api" in main_client.list_branches()

    branch_client = OmnigraphClient(store, cfg.queries_dir, branch="feature_new-api")
    on_branch = branch_client.read(
        "code_read.gq", "find_by_name", {"name": "branch_only_symbol"}
    )
    assert on_branch, "branch view should contain the branch-only symbol"
    # The fork copied main, so main's symbols are visible on the branch too.
    assert branch_client.read("code_read.gq", "find_by_name", {"name": "helper"})

    on_main = main_client.read(
        "code_read.gq", "find_by_name", {"name": "branch_only_symbol"}
    )
    assert not on_main, "main view must not see in-flight branch symbols"


@requires_stack
def test_branch_index_skips_bridge_store(tmp_path, monkeypatch):
    from witan_code import config as cfg_module
    from witan_code import indexer

    monkeypatch.setenv("WITAN_REPO", REPO)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    cfg = cfg_module.load()

    base = _git_repo(tmp_path / "r")
    (base / "svc.py").write_text(SAMPLE)
    indexer.index_path(base, config=cfg)

    _git(base, "checkout", "-q", "-b", "feature/pkg")
    (base / "package.json").write_text('{"name": "@mitodl/branch-pkg"}')
    stats = indexer.index_path(base, config=cfg)

    assert stats.bindings == 0
    assert not cfg_module.bridge_store_path(cfg.code_dir).exists(), (
        "branch index must not create/write the shared bridge store"
    )
