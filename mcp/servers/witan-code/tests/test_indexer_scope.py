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


# ── _collect_files in a git work tree: git's ignore rules apply ──────────────


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _work_tree(tmp_path: Path) -> Path:
    src = tmp_path / "repo"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    return src


def test_a_git_ignored_directory_is_not_collected(tmp_path):
    """A virtualenv under a name `_SKIP_DIRS` does not know. uv and
    `python -m venv` both write a `*` .gitignore into the environment, so git
    ignores it under any name, with nothing in the repo's own .gitignore."""
    src = _work_tree(tmp_path)
    (src / "a.py").write_text("x = 1\n")
    site = src / "env" / "lib" / "python3.12" / "site-packages" / "dep"
    site.mkdir(parents=True)
    (site / "mod.py").write_text("x = 1\n")
    (src / "env" / ".gitignore").write_text("*\n")
    (src / ".gitignore").write_text("out/\n")
    (src / "out").mkdir()
    (src / "out" / "gen.js").write_text("var x = 1;\n")

    assert indexer._collect_files(src) == [src / "a.py"]


def test_untracked_files_that_are_not_ignored_are_collected(tmp_path):
    """A new file nobody has `git add`ed yet is part of the work in progress
    and has to be indexed; so does a tracked file a pattern happens to match,
    which git keeps tracking."""
    src = _work_tree(tmp_path)
    (src / ".gitignore").write_text("generated/\n")
    (src / "tracked.py").write_text("x = 1\n")
    (src / "generated").mkdir()
    (src / "generated" / "kept.py").write_text("x = 1\n")
    _git(src, "add", "-f", "tracked.py", "generated/kept.py")
    (src / "pkg").mkdir()
    (src / "pkg" / "new.py").write_text("x = 1\n")
    (src / "pkg" / "with space.py").write_text("x = 1\n")
    (src / "pkg" / "new\nline.py").write_text("x = 1\n")

    assert indexer._collect_files(src) == sorted(
        [
            src / "generated" / "kept.py",
            src / "pkg" / "new.py",
            src / "pkg" / "new\nline.py",
            src / "pkg" / "with space.py",
            src / "tracked.py",
        ]
    )


def test_git_listing_keeps_the_walk_s_exclusions(tmp_path):
    """git's listing is narrowed, never widened: a tracked file under a
    `_SKIP_DIRS` name, a nested repository (real or not), and a tracked file
    deleted from the work tree all stay out."""
    src = _work_tree(tmp_path)
    (src / "a.py").write_text("x = 1\n")
    (src / "build").mkdir()
    (src / "build" / "b.py").write_text("x = 1\n")
    (src / "gone.py").write_text("x = 1\n")
    _git(src, "add", "build/b.py", "gone.py")
    (src / "gone.py").unlink()
    nested = src / "vendored"
    nested.mkdir()
    _git(nested, "init", "-q")
    (nested / "dep.py").write_text("x = 1\n")
    wt = src / ".claude" / "worktrees" / "feature-x"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /somewhere/.git/worktrees/feature-x\n")
    (wt / "copy.py").write_text("x = 1\n")

    assert indexer._collect_files(src) == [src / "a.py"]


def test_outside_a_work_tree_the_disk_is_walked(tmp_path):
    """No git checkout, so no ignore rules: a .gitignore is just a file."""
    (tmp_path / ".gitignore").write_text("env/\n")
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "e.py").write_text("x = 1\n")
    (tmp_path / "a.py").write_text("x = 1\n")

    assert indexer._collect_files(tmp_path) == [
        tmp_path / "a.py",
        tmp_path / "env" / "e.py",
    ]


def test_without_a_git_binary_the_disk_is_walked(tmp_path, monkeypatch):
    src = _work_tree(tmp_path)
    (src / ".gitignore").write_text("env/\n")
    (src / "env").mkdir()
    (src / "env" / "e.py").write_text("x = 1\n")
    (src / "a.py").write_text("x = 1\n")
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    assert indexer._collect_files(src) == [src / "a.py", src / "env" / "e.py"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 directory")
def test_a_directory_git_cannot_open_is_reported(tmp_path):
    """git warns about it and still exits 0 with the directory's untracked
    files missing, which the purge would read as deleted."""
    src = _work_tree(tmp_path)
    (src / "a.py").write_text("x = 1\n")
    locked = src / "locked"
    locked.mkdir()
    (locked / "b.py").write_text("x = 1\n")
    locked.chmod(0)
    errors: list[OSError] = []
    try:
        collected = indexer._collect_files(src, on_error=errors.append)
    finally:
        locked.chmod(0o755)

    assert collected == [src / "a.py"]
    assert errors


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
def test_a_batch_of_edits_is_one_delete_and_one_load(tmp_path, monkeypatch):
    """What the PostToolUse drainer relies on to bound its writes: edited,
    created and deleted files land together, in the same two commits on the
    repo's store that one edited file costs. Counted at `load_batch`, the
    call that commits, so a load split into node and edge batches (or
    followed by a content-hash load) shows up as the extra commit it is."""
    from witan_code import config as cfg_mod
    from witan_code import store as store_mod
    from witan_code.graph import OmnigraphClient

    src = _repo(tmp_path, monkeypatch)
    for name in ("b", "c", "gone"):
        (src / f"{name}.py").write_text(f"def {name}():\n    return 1\n")
    cfg = cfg_mod.load()
    indexer.index_path(src, config=cfg)
    store = store_mod.store_for_repo("https://github.com/test/cg", cfg)

    writes: list[str] = []
    for method in ("change_many", "load_batch"):
        real = getattr(OmnigraphClient, method)

        def _counted(self, *args, _real=real, _method=method, **kwargs):
            if self.graph_uri == str(store) and args[0]:
                writes.append(_method)
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(OmnigraphClient, method, _counted)

    (src / "a.py").write_text("def a():\n    return 2\n")
    (src / "b.py").write_text("def b():\n    return 2\n")
    (src / "gone.py").unlink()
    (src / "new.py").write_text("def new():\n    return 1\n")
    stats = indexer.index_paths(
        [src / "a.py", src / "b.py", src / "gone.py", src / "new.py"], config=cfg
    )

    assert writes == ["change_many", "load_batch"]
    assert (stats.indexed, stats.purged) == (3, 1)
    assert _files(store, cfg) == {"a.py", "b.py", "c.py", "new.py"}


@requires_stack
def test_a_vanished_file_batched_with_the_repo_root_is_purged_once(
    tmp_path, monkeypatch
):
    """The drainer can hold SessionStart's project dir and a since-deleted
    file in one batch; the full-repo purge covers the file on its own."""
    from witan_code import config as cfg_mod
    from witan_code import store as store_mod

    src = _repo(tmp_path, monkeypatch)
    (src / "gone.py").write_text("def gone():\n    return 1\n")
    cfg = cfg_mod.load()
    indexer.index_path(src, config=cfg)
    store = store_mod.store_for_repo("https://github.com/test/cg", cfg)

    (src / "gone.py").unlink()
    stats = indexer.index_paths([src, src / "gone.py"], config=cfg)

    assert stats.purged == 1
    assert _files(store, cfg) == {"a.py"}


def test_a_batch_spanning_two_checkouts_is_refused(tmp_path, monkeypatch):
    src = _repo(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=other, check=True)
    (other / "b.py").write_text("def b():\n    return 1\n")

    with pytest.raises(ValueError, match="one git checkout"):
        indexer.index_paths([src / "a.py", other / "b.py"])


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
    # hands the failure to onerror, exactly as a PermissionError does. The
    # walk runs when git cannot list the tree; the git listing's own case is
    # the test below.
    monkeypatch.setattr(indexer.repo_module, "unignored_files", lambda *a, **k: None)
    real_walk = indexer.os.walk

    def _walk(top, *args, onerror=None, **kwargs):
        for entry in real_walk(top, *args, **kwargs):
            root, dirs, _files = entry
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


@requires_stack
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 directory")
def test_a_directory_git_cannot_open_suppresses_the_purge(tmp_path, monkeypatch):
    """The same guard as above, for the git listing: git leaves an unreadable
    directory's untracked files out and still exits 0."""
    from witan_code import config as cfg_mod
    from witan_code import store as store_mod

    src = _repo(tmp_path, monkeypatch)
    (src / "sub").mkdir()
    (src / "sub" / "b.py").write_text("def b():\n    return 1\n")
    cfg = cfg_mod.load()
    indexer.index_path(src, config=cfg)
    store = store_mod.store_for_repo("https://github.com/test/cg", cfg)

    (src / "sub").chmod(0)
    try:
        stats = indexer.index_path(src, config=cfg)
    finally:
        (src / "sub").chmod(0o755)

    assert stats.errors >= 1
    assert stats.purged == 0
    assert _files(store, cfg) == {"a.py", "sub/b.py"}


@requires_stack
def test_full_index_purges_rows_for_files_that_became_ignored(tmp_path, monkeypatch):
    """How a store indexed before this rule existed sheds its virtualenv: the
    next full index collects without it, and the purge takes its rows."""
    from witan_code import config as cfg_mod
    from witan_code import store as store_mod

    src = _repo(tmp_path, monkeypatch)
    (src / "env").mkdir()
    (src / "env" / "dep.py").write_text("def dep():\n    return 1\n")
    cfg = cfg_mod.load()
    indexer.index_path(src, config=cfg)
    store = store_mod.store_for_repo("https://github.com/test/cg", cfg)
    assert _files(store, cfg) == {"a.py", "env/dep.py"}

    (src / "env" / ".gitignore").write_text("*\n")
    stats = indexer.index_path(src, config=cfg)

    assert stats.purged == 1
    assert _files(store, cfg) == {"a.py"}


@requires_stack
def test_an_ignored_file_named_directly_is_not_indexed(tmp_path, monkeypatch):
    """The edit hook names files, not directories, so the listing never sees
    them; the batch is held to the same rule in one `git check-ignore`."""
    from witan_code import config as cfg_mod
    from witan_code import store as store_mod

    src = _repo(tmp_path, monkeypatch)
    (src / ".gitignore").write_text("scratch/\n")
    (src / "scratch").mkdir()
    (src / "scratch" / "s.py").write_text("def s():\n    return 1\n")
    (src / "b.py").write_text("def b():\n    return 1\n")
    cfg = cfg_mod.load()

    stats = indexer.index_paths(
        [src / "scratch" / "s.py", src / "b.py", src / "a.py"], config=cfg
    )

    store = store_mod.store_for_repo("https://github.com/test/cg", cfg)
    assert stats.indexed == 2
    assert _files(store, cfg) == {"a.py", "b.py"}


@requires_stack
def test_an_ignored_file_named_directly_drops_rows_it_already_has(
    tmp_path, monkeypatch
):
    """Rows written before the file was ignored go on its next edit, without
    waiting for a full run that may never be allowed to purge the view."""
    from witan_code import config as cfg_mod
    from witan_code import store as store_mod

    src = _repo(tmp_path, monkeypatch)
    (src / "env").mkdir()
    (src / "env" / "dep.py").write_text("def dep():\n    return 1\n")
    cfg = cfg_mod.load()
    indexer.index_path(src, config=cfg)
    store = store_mod.store_for_repo("https://github.com/test/cg", cfg)
    assert _files(store, cfg) == {"a.py", "env/dep.py"}

    (src / "env" / ".gitignore").write_text("*\n")
    stats = indexer.index_paths([src / "env" / "dep.py"], config=cfg)

    assert (stats.indexed, stats.purged) == (0, 1)
    assert _files(store, cfg) == {"a.py"}


@requires_stack
def test_a_file_named_directly_is_held_to_the_walks_path_rules(tmp_path, monkeypatch):
    """An edit under `build/` or a nested checkout writes nothing, so the next
    full index has nothing of it to purge. The `.git` here is one git cannot
    open: a file in a real nested repository has that repository as its root,
    so indexes into its own graph."""
    from witan_code import config as cfg_mod
    from witan_code import store as store_mod

    src = _repo(tmp_path, monkeypatch)
    (src / "build").mkdir()
    (src / "build" / "gen.py").write_text("def gen():\n    return 1\n")
    (src / "vendored").mkdir()
    (src / "vendored" / ".git").mkdir()
    (src / "vendored" / "dep.py").write_text("def dep():\n    return 1\n")
    (src / ".gitignore").write_text("scratch/\n")
    (src / "scratch").mkdir()
    (src / "scratch" / "s.py").write_text("def s():\n    return 1\n")
    cfg = cfg_mod.load()

    stats = indexer.index_paths(
        [
            src / "build" / "gen.py",
            src / "vendored" / "dep.py",
            src / "scratch" / "s.py",
            src / "a.py",
        ],
        config=cfg,
    )

    store = store_mod.store_for_repo("https://github.com/test/cg", cfg)
    assert (stats.indexed, stats.purged) == (1, 0)
    assert _files(store, cfg) == {"a.py"}
    assert indexer.index_path(src, config=cfg).purged == 0


@requires_stack
def test_a_symlink_into_an_excluded_path_is_named_as_the_walk_names_it(
    tmp_path, monkeypatch
):
    """The walk collects `alias.py` though it points into `build/`, and stores
    it under the resolved id. Checking the resolved path instead would drop on
    every edit the rows each full index writes back."""
    from witan_code import config as cfg_mod
    from witan_code import store as store_mod

    src = _repo(tmp_path, monkeypatch)
    (src / "build").mkdir()
    (src / "build" / "real.py").write_text("def real():\n    return 1\n")
    (src / "alias.py").symlink_to(src / "build" / "real.py")
    cfg = cfg_mod.load()
    indexer.index_path(src, config=cfg)
    store = store_mod.store_for_repo("https://github.com/test/cg", cfg)
    assert _files(store, cfg) == {"a.py", "build/real.py"}

    stats = indexer.index_paths([src / "alias.py"], config=cfg, force=True)

    assert (stats.indexed, stats.purged) == (1, 0)
    assert _files(store, cfg) == {"a.py", "build/real.py"}


@requires_stack
def test_a_file_named_through_a_link_from_outside_the_repo_is_indexed(
    tmp_path, monkeypatch
):
    """The link's own name is not in the repo, so the rules see the file."""
    from witan_code import config as cfg_mod
    from witan_code import store as store_mod

    src = _repo(tmp_path, monkeypatch)
    (tmp_path / "link.py").symlink_to(src / "a.py")
    cfg = cfg_mod.load()

    stats = indexer.index_paths([tmp_path / "link.py"], config=cfg)

    store = store_mod.store_for_repo("https://github.com/test/cg", cfg)
    assert stats.indexed == 1
    assert _files(store, cfg) == {"a.py"}


@requires_stack
def test_an_excluded_link_keeps_the_rows_of_a_file_the_walk_collects(
    tmp_path, monkeypatch
):
    """`build/alias.py` is skipped, but the walk collects `a.py` under its own
    name, so the edit must not drop the rows the next full index writes back."""
    from witan_code import config as cfg_mod
    from witan_code import store as store_mod

    src = _repo(tmp_path, monkeypatch)
    (src / "build").mkdir()
    (src / "build" / "alias.py").symlink_to(Path("..") / "a.py")
    cfg = cfg_mod.load()
    indexer.index_path(src, config=cfg)
    store = store_mod.store_for_repo("https://github.com/test/cg", cfg)
    assert _files(store, cfg) == {"a.py"}

    stats = indexer.index_paths([src / "build" / "alias.py"], config=cfg, force=True)

    assert (stats.indexed, stats.purged) == (0, 0)
    assert _files(store, cfg) == {"a.py"}


@pytest.mark.parametrize("through_alias", [False, True])
@requires_stack
def test_a_file_named_below_a_symlinked_directory_is_skipped(
    tmp_path, monkeypatch, through_alias
):
    """The walk never descends `link/`, so `link/mod.py` is not a name it
    collects, also when the checkout itself is reached through a link from
    outside. The rows `src/mod.py` has under its own name stay."""
    from witan_code import config as cfg_mod
    from witan_code import store as store_mod

    src = _repo(tmp_path, monkeypatch)
    (src / "src").mkdir()
    (src / "src" / "mod.py").write_text("def mod():\n    return 1\n")
    (src / "link").symlink_to("src")
    (tmp_path / "checkout").symlink_to(src)
    cfg = cfg_mod.load()
    indexer.index_path(src, config=cfg)
    store = store_mod.store_for_repo("https://github.com/test/cg", cfg)
    assert _files(store, cfg) == {"a.py", "src/mod.py"}

    named = (tmp_path / "checkout" if through_alias else src) / "link" / "mod.py"
    stats = indexer.index_paths([named], config=cfg, force=True)

    assert (stats.indexed, stats.purged) == (0, 0)
    assert _files(store, cfg) == {"a.py", "src/mod.py"}


@requires_stack
def test_an_excluded_file_named_directly_drops_rows_it_already_has(
    tmp_path, monkeypatch
):
    """Rows written before a path rule applied go on the file's next edit, as
    the next full index would drop them."""
    from witan_code import config as cfg_mod
    from witan_code import store as store_mod

    src = _repo(tmp_path, monkeypatch)
    (src / "lib").mkdir()
    (src / "lib" / "dep.py").write_text("def dep():\n    return 1\n")
    cfg = cfg_mod.load()
    indexer.index_path(src, config=cfg)
    store = store_mod.store_for_repo("https://github.com/test/cg", cfg)
    assert _files(store, cfg) == {"a.py", "lib/dep.py"}

    (src / "lib" / ".git").mkdir()
    stats = indexer.index_paths([src / "lib" / "dep.py"], config=cfg)

    assert (stats.indexed, stats.purged) == (0, 1)
    assert _files(store, cfg) == {"a.py"}


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

    Reaching the server is the one thing a client genuinely cannot do without
    one, so only the probe's single round trip is stubbed — on the real client
    class, so everything downstream of it (the graph id, the
    ``--server/--graph`` split, the token, the write guard) is exercised for
    real.
    """
    from witan_code import config as cfg_module
    from witan_code import store as store_mod

    monkeypatch.setenv("WITAN_CODE_SERVER", "https://omnigraph.test")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    declared = frozenset({cfg_module.graph_id(repo), cfg_module.BRIDGE_GRAPH_ID})

    def _list_branches(self):
        if self.graph_id not in declared:
            # The server's own words for a graph it does not serve — the string
            # `probe_cluster_graph` keys "not provisioned" off of.
            raise RuntimeError(f"graph '{self.graph_id}' not found")
        return ["main"]

    monkeypatch.setattr(store_mod.OmnigraphClient, "list_branches", _list_branches)
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
    from witan_core import omnigraph as og

    # Pins the pre-0.14 flags; witan-core's tests cover how 0.14 renders the
    # same server and graph id.
    monkeypatch.setattr(og, "cli_speaks_targets", lambda binary: False)
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


# ── A single file that is gone from disk ─────────────────────────────────────


@requires_stack
def test_indexing_a_deleted_file_clears_its_bridge_bindings(tmp_path, monkeypatch):
    """The per-edit hook indexes a file an agent deleted. Its cross-repo
    bindings must go with its rows, or the bridge keeps reporting a consumer
    that no longer exists."""
    from witan_code import config as cfg_mod
    from witan_code.graph import OmnigraphClient

    src = _repo(tmp_path, monkeypatch)
    (src / "settings.py").write_text(
        'import os\nVALUE = os.getenv("WITAN_CODE_GONE_VAR")\n'
    )
    cfg = cfg_mod.load()
    indexer.index_path(src, config=cfg)
    bridge = OmnigraphClient(
        str(cfg_mod.bridge_store_path(cfg.code_dir)), cfg.queries_dir
    )

    def _binding_files() -> set[str]:
        rows = bridge.read("bridge.gq", "all_bindings", {})
        return {r["file"] for r in rows if r["key_norm"] == "WITAN_CODE_GONE_VAR"}

    assert _binding_files() == {"settings.py"}

    (src / "settings.py").unlink()
    stats = indexer.index_path(src / "settings.py", config=cfg)

    assert stats.purged == 1
    assert _binding_files() == set()


@requires_stack
def test_a_deleted_file_outside_a_git_root_purges_nothing(tmp_path, monkeypatch):
    """`_may_purge`'s guard, for the single-file case: with no git root the
    path is relative to whatever directory is nearest, so under a WITAN_REPO
    override it can name a different file that is still in the repo."""
    from witan_code import config as cfg_mod
    from witan_code import store as store_mod

    src = _repo(tmp_path, monkeypatch)
    cfg = cfg_mod.load()
    indexer.index_path(src, config=cfg)
    store = store_mod.store_for_repo("https://github.com/test/cg", cfg)
    elsewhere = tmp_path / "scratch"  # not a git checkout
    elsewhere.mkdir()

    stats = indexer.index_path(elsewhere / "a.py", config=cfg)

    assert stats.purged == 0
    assert _files(store, cfg) == {"a.py"}
