"""What counts as "this repo's files", and what happens to rows that stop counting.

A linked worktree or submodule under the repo is a *different* checkout, so its
files must not be attributed to this repo — and once excluded, the rows already
written for them have to go, or the store keeps serving stale copies of the
repo to itself (which is exactly how one ended up 74% duplicates).
"""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from witan_code import indexer

from .conftest import requires_stack


def _repo(tmp_path: Path, monkeypatch, name: str = "cg") -> Path:
    """A real git checkout — purging is gated on a confirmed git root, so a
    bare directory would (correctly) never purge and the tests would pass
    vacuously."""
    monkeypatch.setenv("WITAN_REPO", f"https://github.com/test/{name}")
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    src = tmp_path / "repo"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=src, check=True)
    (src / "a.py").write_text("def a():\n    return 1\n")
    return src


def _files(store, cfg) -> set[str]:
    """Repo-relative paths currently indexed in ``store``."""
    from witan_code.graph import OmnigraphClient

    rows = OmnigraphClient(str(store), cfg.queries_dir).read(
        "code_read.gq", "all_file_hashes", {}
    )
    return {r["slug"].split("#", 1)[1] for r in rows}


# ── _collect_files: what the walk descends into ──────────────────────────────


def test_nested_worktree_is_not_collected(tmp_path):
    """A linked worktree's `.git` is a FILE, not a directory — the `.git` entry
    in _SKIP_DIRS never matched it, which is how these got indexed."""
    (tmp_path / "a.py").write_text("x = 1\n")
    wt = tmp_path / ".claude" / "worktrees" / "feature-x"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /somewhere/.git/worktrees/feature-x\n")
    (wt / "copy.py").write_text("x = 1\n")

    collected = indexer._collect_files(tmp_path)

    assert tmp_path / "a.py" in collected
    assert not any("worktrees" in p.as_posix() for p in collected)


def test_nested_clone_with_a_git_directory_is_not_collected(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    vendored = tmp_path / "vendored"
    (vendored / ".git").mkdir(parents=True)
    (vendored / "dep.py").write_text("x = 1\n")

    collected = indexer._collect_files(tmp_path)

    assert collected == [tmp_path / "a.py"]


def test_indexing_from_inside_a_worktree_still_works(tmp_path):
    """The target root is never a pruning candidate — only descending into a
    nested checkout from outside is refused. The hooks index from inside a
    worktree whenever an agent works there, so this must keep collecting."""
    wt = tmp_path / "worktrees" / "feature-x"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /somewhere\n")
    (wt / "own.py").write_text("x = 1\n")

    assert indexer._collect_files(wt) == [wt / "own.py"]


def test_skip_dirs_are_pruned_not_merely_filtered(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    junk = tmp_path / "node_modules" / "pkg"
    junk.mkdir(parents=True)
    (junk / "index.js").write_text("var x = 1;\n")

    assert indexer._collect_files(tmp_path) == [tmp_path / "a.py"]


def test_collection_order_is_deterministic(tmp_path):
    for name in ("c.py", "a.py", "b.py"):
        (tmp_path / name).write_text("x = 1\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "d.py").write_text("x = 1\n")

    assert indexer._collect_files(tmp_path) == sorted(indexer._collect_files(tmp_path))


# ── Purging rows that are no longer this repo's ──────────────────────────────


@requires_stack
def test_full_index_purges_a_file_deleted_from_disk(tmp_path, monkeypatch):
    from witan_code import config as cfg_mod
    from witan_code import store as store_mod

    src = _repo(tmp_path, monkeypatch)
    (src / "gone.py").write_text("def gone():\n    return 1\n")
    cfg = cfg_mod.load()
    indexer.index_path(src, config=cfg)
    store = store_mod.store_for_repo("https://github.com/test/cg", cfg)
    assert _files(store, cfg) == {"a.py", "gone.py"}

    (src / "gone.py").unlink()
    stats = indexer.index_path(src, config=cfg)

    assert stats.purged == 1
    assert _files(store, cfg) == {"a.py"}


@requires_stack
def test_full_index_purges_files_that_became_a_nested_checkout(tmp_path, monkeypatch):
    """The case an on-disk existence check cannot catch: the files are still
    there, they just stopped being this repo's."""
    from witan_code import config as cfg_mod
    from witan_code import store as store_mod

    src = _repo(tmp_path, monkeypatch)
    nested = src / "sub"
    nested.mkdir()
    (nested / "b.py").write_text("def b():\n    return 1\n")
    cfg = cfg_mod.load()
    indexer.index_path(src, config=cfg)
    store = store_mod.store_for_repo("https://github.com/test/cg", cfg)
    assert _files(store, cfg) == {"a.py", "sub/b.py"}

    # `sub/` becomes a linked worktree; its file is untouched on disk.
    (nested / ".git").write_text("gitdir: /somewhere\n")
    stats = indexer.index_path(src, config=cfg)

    assert (nested / "b.py").exists()
    assert stats.purged == 1
    assert _files(store, cfg) == {"a.py"}


@requires_stack
def test_indexing_a_subpath_purges_nothing(tmp_path, monkeypatch):
    """Everything outside the subpath is legitimately uncollected — treating
    that as stale would empty the store on every PostToolUse single-file run."""
    from witan_code import config as cfg_mod
    from witan_code import store as store_mod

    src = _repo(tmp_path, monkeypatch)
    sub = src / "pkg"
    sub.mkdir()
    (sub / "b.py").write_text("def b():\n    return 1\n")
    cfg = cfg_mod.load()
    indexer.index_path(src, config=cfg)
    store = store_mod.store_for_repo("https://github.com/test/cg", cfg)

    stats = indexer.index_path(sub, config=cfg)

    assert stats.purged == 0
    assert _files(store, cfg) == {"a.py", "pkg/b.py"}


@requires_stack
def test_purge_survives_a_forced_reindex(tmp_path, monkeypatch):
    """`force` skips the hash comparison but must still read the stored file
    set — otherwise a --force run silently stops purging."""
    from witan_code import config as cfg_mod
    from witan_code import store as store_mod

    src = _repo(tmp_path, monkeypatch)
    (src / "gone.py").write_text("def gone():\n    return 1\n")
    cfg = cfg_mod.load()
    indexer.index_path(src, config=cfg)
    store = store_mod.store_for_repo("https://github.com/test/cg", cfg)

    (src / "gone.py").unlink()
    stats = indexer.index_path(src, force=True, config=cfg)

    assert stats.purged == 1
    assert _files(store, cfg) == {"a.py"}


@requires_stack
def test_no_purge_without_a_confirmed_git_root(tmp_path, monkeypatch):
    """The guard on the destructive path.

    Without git, `base` falls back to the target directory, so `full_repo` is
    true for ANY directory indexed. Combined with a WITAN_REPO override (one
    slug, two different bases) an unguarded purge would delete every row whose
    path was stored relative to the real root — i.e. empty the store.
    """
    from witan_code import config as cfg_mod
    from witan_code import store as store_mod

    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/nogit")
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    src = tmp_path / "plain"  # deliberately NOT a git checkout
    (src / "pkg").mkdir(parents=True)
    (src / "a.py").write_text("def a():\n    return 1\n")
    (src / "pkg" / "b.py").write_text("def b():\n    return 1\n")

    cfg = cfg_mod.load()
    indexer.index_path(src, config=cfg)
    store = store_mod.store_for_repo("https://github.com/test/nogit", cfg)
    assert _files(store, cfg) == {"a.py", "pkg/b.py"}

    stats = indexer.index_path(src / "pkg", config=cfg)

    assert stats.purged == 0
    # Both original rows survive — nothing was deleted, which is the point.
    # This run also ADDS a duplicate `b.py` (paths relative to the subdirectory
    # base rather than the real root): a pre-existing quirk of overriding
    # WITAN_REPO without a git root, unrelated to purging. Asserted as a subset
    # so this test pins the guard and not that quirk.
    assert {"a.py", "pkg/b.py"} <= _files(store, cfg)


@requires_stack
def test_unreadable_directory_suppresses_the_purge(tmp_path, monkeypatch):
    """A subtree the walk cannot read looks exactly like a deleted one.

    os.walk reports such a directory to `onerror` and otherwise carries on
    silently, so without this guard an unreadable subtree would take its
    still-present files' rows with it — a permission blip turning into data
    loss. Indexing still proceeds with whatever was readable.
    """
    from witan_code import config as cfg_mod
    from witan_code import store as store_mod

    src = _repo(tmp_path, monkeypatch)
    (src / "sub").mkdir()
    (src / "sub" / "b.py").write_text("def b():\n    return 1\n")
    cfg = cfg_mod.load()
    indexer.index_path(src, config=cfg)
    store = store_mod.store_for_repo("https://github.com/test/cg", cfg)
    assert _files(store, cfg) == {"a.py", "sub/b.py"}

    # Simulate `sub/` becoming unreadable: os.walk yields the root only and
    # hands the failure to onerror, exactly as a PermissionError does.
    real_walk = indexer.os.walk

    def _walk(top, *args, onerror=None, **kwargs):
        for entry in real_walk(top, *args, **kwargs):
            root, dirs, files = entry
            if Path(root) == src:
                dirs[:] = [d for d in dirs if d != "sub"]
                if onerror is not None:
                    onerror(PermissionError(13, "Permission denied", str(src / "sub")))
            yield entry

    monkeypatch.setattr(indexer.os, "walk", _walk)
    stats = indexer.index_path(src, config=cfg)

    assert stats.errors >= 1  # the unreadable directory is reported, not hidden
    assert stats.purged == 0
    assert _files(store, cfg) == {"a.py", "sub/b.py"}


# ── _may_purge: the gate on the one destructive step ─────────────────────────


def _cfg(role: str = "client"):
    from witan_code import config as cfg_mod

    return cfg_mod.Config(
        code_dir=Path("/code"),
        author="test",
        queries_dir=Path("/queries"),
        schema_file=Path("/schema.pg"),
        bridge_schema_file=Path("/bridge.pg"),
        index_role=role,
    )


def _gate(**overrides):
    """Call `_may_purge` with an authoritative baseline, overriding one clause."""
    kwargs = {
        "full_repo": True,
        "repo_root": Path("/repo"),
        "walk_errors": [],
        "client": SimpleNamespace(is_remote=False),
        "branch": None,
        "cfg": _cfg(),
        "actor": None,
    }
    kwargs.update(overrides)
    return indexer._may_purge(**kwargs)


def test_purge_allowed_only_when_this_machine_is_authoritative():
    assert _gate() is True

    # A subpath run collects a fraction of the repo by design.
    assert _gate(full_repo=False) is False
    # No git root => `base` is the target dir, so `full_repo` means nothing.
    assert _gate(repo_root=None) is False
    # An unreadable subtree looks exactly like a deleted one.
    assert _gate(walk_errors=[PermissionError(13, "denied", "/repo/sub")]) is False


def test_purge_refused_against_a_shared_cluster_graph():
    """A remote graph's default view is shared: it is indexed by CI and
    everyone else reads it. Reconciling it against one developer's working
    tree — sparse checkout, stale checkout, uncommitted deletions — would
    purge files for every other user of that graph.
    """
    assert _gate(client=SimpleNamespace(is_remote=True)) is False


def test_designated_writer_may_purge_a_shared_cluster_graph():
    """CI owns the shared default-branch view, and dropping rows for files
    deleted from the default branch is precisely its job. The right comes from
    the declared role, not from the transport — CI is remote like everyone."""
    assert _gate(client=SimpleNamespace(is_remote=True), cfg=_cfg("ci")) is True


def test_an_actor_may_purge_its_own_branch_view():
    """The case the old "remote and not the designated writer" rule got wrong.
    A branch view has exactly one writer; refusing them the purge left files
    they had deleted lingering in their own view."""
    assert (
        _gate(
            client=SimpleNamespace(is_remote=True),
            branch="act-alice/feature-x",
            actor="act-alice",
        )
        is True
    )


def test_no_purging_of_a_branch_view_this_actor_does_not_own():
    remote = SimpleNamespace(is_remote=True)
    assert _gate(client=remote, branch="act-bob/feature-x", actor="act-alice") is False
    # Un-owned, so nobody's to reconcile.
    assert _gate(client=remote, branch="feature-x", actor="act-alice") is False


def test_ownership_does_not_override_the_other_clauses():
    """It answers only "is this view mine to reconcile" — it says nothing
    about whether this run's file listing is complete."""
    remote = SimpleNamespace(is_remote=True)
    ci = _cfg("ci")
    assert _gate(client=remote, cfg=ci, full_repo=False) is False
    assert _gate(client=remote, cfg=ci, repo_root=None) is False
    assert (
        _gate(
            client=remote,
            cfg=ci,
            walk_errors=[PermissionError(13, "denied", "/repo/sub")],
        )
        is False
    )


# ── The gate is wired into index_path, not just testable in isolation ────────


@requires_stack
def test_index_path_refuses_a_shared_default_branch_view(tmp_path, monkeypatch):
    """Refused before anything is written, not part-way through.

    Driven through the real cluster addressing (``code_server`` + the
    provisioned graph id) rather than by forcing ``is_remote`` on the client:
    the point of the gate is that pointing an ordinary developer's indexer at
    the deployed server does not let it overwrite CI's view, and that is only
    actually tested if the store resolves to the server the way it would in
    production.
    """
    from witan_code import store as store_mod
    from witan_code.graph import SharedGraphWriteRefused

    src = _repo(tmp_path, monkeypatch)
    cfg = _cluster_cfg(monkeypatch, "https://github.com/test/cg")

    with pytest.raises(SharedGraphWriteRefused, match="owned by CI"):
        indexer.index_path(src, config=cfg)

    # Nothing fell back to a local store, and nothing reached the server: the
    # refusal is the whole of what happened.
    assert not (tmp_path / "code").exists()
    assert store_mod.store_for_repo("https://github.com/test/cg", cfg).is_remote


def _cluster_cfg(monkeypatch, repo: str, **env: str):
    """Config addressing a (stubbed) cluster that has ``repo``'s graph declared.

    ``graphs list`` is the one thing a client genuinely cannot do without a
    server; everything downstream of it — the graph id, the ``--server/--graph``
    split, the write guard — is exercised for real.
    """
    from witan_code import config as cfg_module
    from witan_code import store as store_mod

    monkeypatch.setenv("WITAN_CODE_SERVER", "https://omnigraph.test")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    declared = frozenset({cfg_module.graph_id(repo), cfg_module.BRIDGE_GRAPH_ID})
    monkeypatch.setattr(store_mod, "cluster_graphs", lambda *a, **kw: declared)
    return cfg_module.load()


@requires_stack
def test_index_path_addresses_the_provisioned_cluster_graph(tmp_path, monkeypatch):
    """The CI indexer reaches `--server <url> --graph code-…`, not a local path.

    The graph id is the contract shared byte-for-byte with ol-infrastructure's
    provisioning, so this pins the flags the subprocess would actually carry:
    addressing a graph the cluster never declared is the failure this whole
    resolution path exists to prevent.
    """
    from witan_code import config as cfg_module
    from witan_code import store as store_mod

    _repo(tmp_path, monkeypatch)
    cfg = _cluster_cfg(
        monkeypatch,
        "https://github.com/test/cg",
        WITAN_CODE_INDEX_ROLE="ci",
        WITAN_CODE_TOKEN="s3cret",
    )

    ref = store_mod.ensure_store("https://github.com/test/cg", cfg)
    client = ref.client(cfg)

    assert client._store_args() == [
        "--server",
        "https://omnigraph.test",
        "--graph",
        cfg_module.graph_id("https://github.com/test/cg"),
    ]
    assert client.token == "s3cret"
    # CI owns the default view, so the guard that refused above lets this pass.
    assert store_mod.store_for_repo("https://github.com/test/cg", cfg).exists()


@requires_stack
def test_index_path_refuses_an_undeclared_cluster_graph(tmp_path, monkeypatch):
    """A repo provisioning never declared fails loudly, before the first write.

    The alternative is thousands of symbol writes each failing against a graph
    that isn't there and an index that reports success having stored nothing.
    """
    from witan_code import config as cfg_module
    from witan_code import store as store_mod

    src = _repo(tmp_path, monkeypatch)
    # A cluster that declares some other repo's graph, but not this one's.
    cfg = _cluster_cfg(monkeypatch, "https://github.com/test/other")
    monkeypatch.setenv("WITAN_CODE_INDEX_ROLE", "ci")
    cfg = cfg_module.load()

    with pytest.raises(store_mod.ClusterGraphMissing, match="data_tier.py"):
        indexer.index_path(src, config=cfg)


@requires_stack
def test_index_path_passes_the_resolved_role_to_the_purge_gate(tmp_path, monkeypatch):
    """The role has to reach `_may_purge`, or CI silently stops purging — a
    failure whose only symptom is deleted files lingering in the shared view."""
    from witan_code import config as cfg_module

    src = _repo(tmp_path, monkeypatch)
    monkeypatch.setenv("WITAN_CODE_INDEX_ROLE", "ci")

    seen: dict = {}
    real = indexer._may_purge

    def _spy(**kwargs):
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(indexer, "_may_purge", _spy)
    indexer.index_path(src, config=cfg_module.load())

    assert seen["cfg"].is_designated_writer is True


@requires_stack
def test_index_path_passes_the_view_it_wrote_to_the_purge_gate(tmp_path, monkeypatch):
    """The gate decides ownership of a *view*, so it has to be told which one
    was written — not just the git branch, and not nothing."""
    from witan_code import config as cfg_module
    from witan_code import identity as identity_module

    src = _repo(tmp_path, monkeypatch)
    # `git rev-parse --abbrev-ref HEAD` fails on an unborn HEAD, so a branch
    # only becomes visible to `store_branch` once something is committed.
    subprocess.run(["git", "add", "-A"], cwd=src, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "init"],
        cwd=src,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
        },
    )
    subprocess.run(["git", "checkout", "-q", "-b", "feature/x"], cwd=src, check=True)
    monkeypatch.setenv(identity_module.ACTOR_ENV_VAR, "act-alice")
    identity_module.reset_cache()

    seen: dict = {}
    real = indexer._may_purge

    def _spy(**kwargs):
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(indexer, "_may_purge", _spy)
    indexer.index_path(src, config=cfg_module.load())

    assert seen["branch"] == "act-alice/feature_x"
    assert seen["actor"] == "act-alice"
