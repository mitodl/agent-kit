"""What counts as "this repo's files", and what happens to rows that stop counting.

A linked worktree or submodule under the repo is a *different* checkout, so its
files must not be attributed to this repo — and once excluded, the rows already
written for them have to go, or the store keeps serving stale copies of the
repo to itself (which is exactly how one ended up 74% duplicates).
"""

import subprocess
from pathlib import Path

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
