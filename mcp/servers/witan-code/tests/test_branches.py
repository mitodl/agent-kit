"""Tests for branch-aware indexing: git branch → omnigraph branch mapping."""

import asyncio
import inspect
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
    """Unwrap + run a (possibly async) FastMCP tool directly, as the CLI does."""
    fn = getattr(tool, "fn", tool)
    if inspect.iscoroutinefunction(fn):

        def runner(*args, **kwargs):
            return asyncio.run(fn(*args, **kwargs))

        return runner
    return fn


# ── Git-context caching (server.py) ───────────────────────────────


def test_cached_git_amortizes_repeated_calls_within_ttl(monkeypatch):
    """_cached_detect/_cached_store_branch spawn a git subprocess via
    repo_module at most once per TTL window, not once per tool call."""
    from witan_code import server as srv

    srv._git_context.clear()
    calls = {"detect": 0, "store_branch": 0}
    monkeypatch.setattr(
        srv.repo_module,
        "detect",
        lambda: calls.__setitem__("detect", calls["detect"] + 1) or "r",
    )
    monkeypatch.setattr(
        srv.repo_module,
        "store_branch",
        lambda: calls.__setitem__("store_branch", calls["store_branch"] + 1) or "b",
    )

    for _ in range(5):
        assert srv._cached_detect() == "r"
        assert srv._cached_store_branch() == "b"

    assert calls == {"detect": 1, "store_branch": 1}


def test_cached_git_refreshes_after_ttl(monkeypatch):
    from witan_code import server as srv

    srv._git_context.clear()
    values = iter(["a", "b"])
    monkeypatch.setattr(srv.repo_module, "detect", lambda: next(values))

    assert srv._cached_detect() == "a"
    srv._git_context["detect"] = (
        srv._git_context["detect"][0] - srv._GIT_CONTEXT_TTL - 1,
        "a",
    )
    assert srv._cached_detect() == "b"


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
    srv._git_context.clear()

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
    srv._git_context.clear()  # the 2s TTL would otherwise still see feature/pkg
    on_main = _fn(srv.code_interface_providers)("package", "@mitodl/branch-pkg")
    assert on_main == [], "back on main, the in-flight-only binding is invisible"


# ── Per-writer branch views ───────────────────────────────────────
#
# The reason this exists: on a shared graph the git branch alone is not a
# unique key. Two checkouts of `feature-x` — two developers, one developer in
# two worktrees, an agent and its human — are two working trees.


@requires_stack
def test_two_writers_on_one_branch_do_not_overwrite_each_other(tmp_path, monkeypatch):
    """The collision, driven end to end. Both index `feature/shared` into the
    same store; each writer's symbol must survive in its own view."""
    from witan_code import config as cfg_module
    from witan_code import identity as identity_module
    from witan_code import indexer
    from witan_code.graph import OmnigraphClient

    monkeypatch.setenv("WITAN_REPO", REPO)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    cfg = cfg_module.load()

    def _index_as(actor: str, checkout, symbol: str):
        monkeypatch.setenv(identity_module.ACTOR_ENV_VAR, actor)
        identity_module.reset_cache()
        (checkout / "wip.py").write_text(f"def {symbol}():\n    return 1\n")
        indexer.index_path(checkout, config=cfg)

    # Two independent checkouts of the same repo, both on the same branch.
    alice = _git_repo(tmp_path / "alice")
    (alice / "svc.py").write_text(SAMPLE)
    _git(alice, "checkout", "-q", "-b", "feature/shared")
    bob = _git_repo(tmp_path / "bob")
    (bob / "svc.py").write_text(SAMPLE)
    _git(bob, "checkout", "-q", "-b", "feature/shared")

    _index_as("act-alice", alice, "alice_only_symbol")
    _index_as("act-bob", bob, "bob_only_symbol")

    store = str(cfg_module.store_path(REPO, cfg.code_dir))
    names = OmnigraphClient(store, cfg.queries_dir).list_branches()
    assert "act-alice/feature_shared" in names
    assert "act-bob/feature_shared" in names

    def _has(view: str, symbol: str) -> bool:
        client = OmnigraphClient(store, cfg.queries_dir, branch=view)
        return bool(client.read("code_read.gq", "find_by_name", {"name": symbol}))

    assert _has("act-alice/feature_shared", "alice_only_symbol")
    assert _has("act-bob/feature_shared", "bob_only_symbol")
    # The point: neither writer's view was clobbered by the other's.
    assert not _has("act-alice/feature_shared", "bob_only_symbol")
    assert not _has("act-bob/feature_shared", "alice_only_symbol")


@requires_stack
def test_a_reader_can_enumerate_every_writers_view_of_a_branch(tmp_path, monkeypatch):
    """Isolation and visibility are not in tension: the views are separated by
    writer AND every one of them is readable. This is the payoff the "branch
    views live on the shared graph" decision was taken for."""
    from witan_code import config as cfg_mod
    from witan_code import identity as identity_module
    from witan_code import indexer
    from witan_code import server as srv

    monkeypatch.setenv("WITAN_REPO", REPO)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()
    srv._store_branches.clear()
    srv._git_context.clear()

    base = _git_repo(tmp_path / "r")
    (base / "svc.py").write_text(SAMPLE)
    _git(base, "checkout", "-q", "-b", "feature/x")
    for actor in ("act-alice", "act-bob"):
        monkeypatch.setenv(identity_module.ACTOR_ENV_VAR, actor)
        identity_module.reset_cache()
        indexer.index_path(base, config=srv.cfg)

    rows = _fn(srv.code_indexed_branches)(branch="feature/x")

    assert [r["repo"] for r in rows] == [REPO]
    assert [(v["actor"], v["view"]) for v in rows[0]["views"]] == [
        ("act-alice", "act-alice/feature_x"),
        ("act-bob", "act-bob/feature_x"),
    ]


@requires_stack
def test_a_returned_view_is_queryable_as_the_branch_argument(tmp_path, monkeypatch):
    """The listing is only useful if what it returns can be fed straight back
    — that is how an agent reads a teammate's in-flight work."""
    from witan_code import config as cfg_mod
    from witan_code import identity as identity_module
    from witan_code import indexer
    from witan_code import server as srv

    monkeypatch.setenv("WITAN_REPO", REPO)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()
    srv._store_branches.clear()
    srv._git_context.clear()

    base = _git_repo(tmp_path / "r")
    (base / "svc.py").write_text(SAMPLE)
    indexer.index_path(base, config=srv.cfg)

    _git(base, "checkout", "-q", "-b", "feature/x")
    (base / "wip.py").write_text("def bobs_wip_symbol():\n    return 1\n")
    monkeypatch.setenv(identity_module.ACTOR_ENV_VAR, "act-bob")
    identity_module.reset_cache()
    indexer.index_path(base, config=srv.cfg)

    # Read as somebody else entirely, from outside any checkout of the repo.
    monkeypatch.setenv(identity_module.ACTOR_ENV_VAR, "act-alice")
    identity_module.reset_cache()
    srv._git_context.clear()

    found = _fn(srv.code_find_definition)(
        "bobs_wip_symbol", repo=REPO, branch="act-bob/feature_x"
    )
    assert found, "a named view must be queryable by whoever asks"

    on_main = _fn(srv.code_find_definition)("bobs_wip_symbol", repo=REPO)
    assert not on_main, "and must not leak into the view everyone falls back to"


@requires_stack
def test_reads_prefer_this_actors_own_view_of_the_branch(tmp_path, monkeypatch):
    """Default reads follow the checkout's branch — but to MY view of it, not
    a colleague's half-written state."""
    from witan_code import config as cfg_mod
    from witan_code import identity as identity_module
    from witan_code import indexer
    from witan_code import server as srv

    monkeypatch.setenv("WITAN_REPO", REPO)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()
    srv._store_branches.clear()
    srv._git_context.clear()

    base = _git_repo(tmp_path / "r")
    (base / "svc.py").write_text(SAMPLE)
    _git(base, "checkout", "-q", "-b", "feature/x")

    monkeypatch.setenv(identity_module.ACTOR_ENV_VAR, "act-bob")
    identity_module.reset_cache()
    (base / "wip.py").write_text("def bobs_wip_symbol():\n    return 1\n")
    indexer.index_path(base, config=srv.cfg)

    monkeypatch.setenv(identity_module.ACTOR_ENV_VAR, "act-alice")
    identity_module.reset_cache()
    (base / "wip.py").write_text("def alices_wip_symbol():\n    return 1\n")
    indexer.index_path(base, config=srv.cfg)

    monkeypatch.chdir(base)
    srv._clients.clear()
    srv._store_branches.clear()
    srv._git_context.clear()

    assert _fn(srv.code_find_definition)("alices_wip_symbol")
    assert not _fn(srv.code_find_definition)("bobs_wip_symbol")


@requires_stack
def test_reads_fall_back_to_another_writers_view(tmp_path, monkeypatch):
    """Before you have indexed a branch yourself, the closest thing to "the
    code on feature-x" is whoever's view of it does exist. Reading, unlike
    writing, is not the operation that needs an owner."""
    from witan_code import config as cfg_mod
    from witan_code import identity as identity_module
    from witan_code import indexer
    from witan_code import server as srv

    monkeypatch.setenv("WITAN_REPO", REPO)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()
    srv._store_branches.clear()
    srv._git_context.clear()

    base = _git_repo(tmp_path / "r")
    (base / "svc.py").write_text(SAMPLE)
    _git(base, "checkout", "-q", "-b", "feature/x")
    monkeypatch.setenv(identity_module.ACTOR_ENV_VAR, "act-bob")
    identity_module.reset_cache()
    (base / "wip.py").write_text("def bobs_wip_symbol():\n    return 1\n")
    indexer.index_path(base, config=srv.cfg)

    # Alice has never indexed this branch, so she has no view of her own.
    monkeypatch.setenv(identity_module.ACTOR_ENV_VAR, "act-alice")
    identity_module.reset_cache()
    monkeypatch.chdir(base)
    srv._clients.clear()
    srv._store_branches.clear()
    srv._git_context.clear()

    assert _fn(srv.code_find_definition)("bobs_wip_symbol")


@requires_stack
def test_a_branch_name_with_a_slash_is_not_read_as_a_view_name(tmp_path, monkeypatch):
    """`feature/new-api` and `act-bob/feature_x` both contain "/". Only the
    actor prefix tells them apart, and getting that wrong would send every
    request for a slashed git branch to main."""
    from witan_code import config as cfg_mod
    from witan_code import identity as identity_module
    from witan_code import indexer
    from witan_code import server as srv

    monkeypatch.setenv("WITAN_REPO", REPO)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()
    srv._store_branches.clear()
    srv._git_context.clear()

    base = _git_repo(tmp_path / "r")
    (base / "svc.py").write_text(SAMPLE)
    indexer.index_path(base, config=srv.cfg)

    _git(base, "checkout", "-q", "-b", "feature/new-api")
    (base / "wip.py").write_text("def slashed_branch_symbol():\n    return 1\n")
    monkeypatch.setenv(identity_module.ACTOR_ENV_VAR, "act-alice")
    identity_module.reset_cache()
    indexer.index_path(base, config=srv.cfg)

    srv._clients.clear()
    srv._store_branches.clear()
    srv._git_context.clear()

    found = _fn(srv.code_find_definition)(
        "slashed_branch_symbol", repo=REPO, branch="feature/new-api"
    )
    assert found, "a raw git branch with a slash must map through sanitization"
