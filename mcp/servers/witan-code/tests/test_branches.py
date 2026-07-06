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
def test_branch_index_writes_to_bridge_overlay_not_main(tmp_path, monkeypatch):
    """A non-default branch's bridge writes land on its repo-qualified
    overlay branch (docs/BRANCH_INDEXING.md § Bridge store) — visible when
    reading that branch, invisible on bridge main."""
    from witan_code import config as cfg_module
    from witan_code import indexer
    from witan_code.graph import OmnigraphClient

    monkeypatch.setenv("WITAN_REPO", REPO)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    cfg = cfg_module.load()

    base = _git_repo(tmp_path / "r")
    (base / "svc.py").write_text(SAMPLE)
    indexer.index_path(base, config=cfg)

    _git(base, "checkout", "-q", "-b", "feature/pkg")
    (base / "package.json").write_text('{"name": "@mitodl/branch-pkg"}')
    stats = indexer.index_path(base, config=cfg)

    assert stats.bindings > 0
    bridge_store = cfg_module.bridge_store_path(cfg.code_dir)
    assert bridge_store.exists()

    bridge_branch = f"{cfg_module.sanitize_slug(REPO)}/feature_pkg"
    main_client = OmnigraphClient(str(bridge_store), cfg.queries_dir)
    assert bridge_branch in main_client.list_branches()

    main_rows = main_client.read("bridge.gq", "all_bindings", {})
    assert not any(r["key_norm"] == "@mitodl/branch-pkg" for r in main_rows), (
        "in-flight branch binding must not pollute the shared bridge main view"
    )

    branch_client = OmnigraphClient(
        str(bridge_store), cfg.queries_dir, branch=bridge_branch
    )
    branch_rows = branch_client.read("bridge.gq", "all_bindings", {})
    assert any(r["key_norm"] == "@mitodl/branch-pkg" for r in branch_rows), (
        "the repo-qualified overlay branch should see the in-flight binding"
    )


OTHER_REPO = "https://github.com/test/other-cg"


@requires_stack
def test_bridge_overlay_includes_other_repos_main_bindings(tmp_path, monkeypatch):
    """The overlay branch forks from bridge main, so it starts as a full copy:
    a repo's in-flight branch view still sees every OTHER repo's main
    bindings, not just its own in-flight ones."""
    from witan_code import config as cfg_module
    from witan_code import indexer
    from witan_code.graph import OmnigraphClient

    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    cfg = cfg_module.load()

    other = _git_repo(tmp_path / "other")
    (other / "package.json").write_text('{"name": "@mitodl/other-pkg"}')
    monkeypatch.setenv("WITAN_REPO", OTHER_REPO)
    indexer.index_path(other, config=cfg)

    base = _git_repo(tmp_path / "r")
    (base / "svc.py").write_text(SAMPLE)
    monkeypatch.setenv("WITAN_REPO", REPO)
    indexer.index_path(base, config=cfg)

    _git(base, "checkout", "-q", "-b", "feature/pkg")
    (base / "package.json").write_text('{"name": "@mitodl/branch-pkg"}')
    indexer.index_path(base, config=cfg)

    bridge_store = cfg_module.bridge_store_path(cfg.code_dir)
    bridge_branch = f"{cfg_module.sanitize_slug(REPO)}/feature_pkg"
    branch_client = OmnigraphClient(
        str(bridge_store), cfg.queries_dir, branch=bridge_branch
    )
    branch_rows = branch_client.read("bridge.gq", "all_bindings", {})
    key_norms = {r["key_norm"] for r in branch_rows}
    assert "@mitodl/branch-pkg" in key_norms, "this repo's own in-flight binding"
    assert "@mitodl/other-pkg" in key_norms, (
        "the OTHER repo's main binding should still be visible on the overlay"
    )


def _fn(tool):
    return getattr(tool, "fn", tool)


@requires_stack
def test_bridge_client_follows_current_checkout_branch(tmp_path, monkeypatch):
    """code_interface_providers auto-detects the cwd's repo+branch: sees the
    in-flight overlay while checked out on the branch, falls back to bridge
    main (which doesn't have it) once back on the default branch."""
    from witan_code import config as cfg_mod
    from witan_code import indexer
    from witan_code import server as srv

    monkeypatch.setenv("WITAN_REPO", REPO)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()
    srv._store_branches.clear()

    base = _git_repo(tmp_path / "r")
    (base / "svc.py").write_text(SAMPLE)
    indexer.index_path(base, config=srv.cfg)

    _git(base, "checkout", "-q", "-b", "feature/pkg")
    (base / "package.json").write_text('{"name": "@mitodl/branch-pkg"}')
    indexer.index_path(base, config=srv.cfg)

    monkeypatch.chdir(base)
    on_branch = _fn(srv.code_interface_providers)("package", "@mitodl/branch-pkg")
    assert any(p["repo"] == REPO for p in on_branch), (
        "cwd on the feature branch should see its own overlay"
    )

    _git(base, "checkout", "-q", "main")
    on_main = _fn(srv.code_interface_providers)("package", "@mitodl/branch-pkg")
    assert on_main == [], "back on main, the in-flight-only binding is invisible"
