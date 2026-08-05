"""A reindex must clear stale rows in ONE commit, not two per file.

The insert side has always been bulk (`client.load`). The DELETE side was not:
`_delete_file_data` issued two `mutate` calls per changed-or-purged file, so a
200-file reindex cost 400 Lance versions — which made a reindex the largest
fragmentation source in the store the bulk load exists to protect.
"""

import subprocess
from pathlib import Path

import pytest

from .conftest import requires_stack


@pytest.fixture
def git_repo(tmp_path, monkeypatch) -> Path:
    """A real git checkout — purging is gated on a confirmed git root, so a
    bare directory would (correctly) never purge and the test would pass
    vacuously."""
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/cgdel")
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    src = tmp_path / "repo"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=src, check=True)
    (src / "keep.py").write_text("def keep():\n    return 1\n")
    return src


@pytest.fixture
def mutates(monkeypatch):
    """Count `omnigraph mutate` subprocess invocations while in scope."""
    real_run = subprocess.run
    calls: list[list[str]] = []

    def counting_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and "mutate" in cmd:
            calls.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)

    class Counter:
        def reset(self):
            calls.clear()

        @property
        def count(self):
            return len(calls)

    return Counter()


@requires_stack
def test_reindexing_many_changed_files_is_one_delete_commit(
    sample_repo, mutates, tmp_path
):
    from witan_code import config as cfg_mod
    from witan_code import indexer

    cfg = cfg_mod.load()
    for i in range(5):
        (sample_repo / f"mod{i}.py").write_text(f"def f{i}():\n    return {i}\n")
    first = indexer.index_path(sample_repo, config=cfg)
    assert first.indexed >= 6

    # Change every file so all of them are REINDEXED (new files delete nothing).
    for i in range(5):
        (sample_repo / f"mod{i}.py").write_text(f"def f{i}():\n    return {i} + 1\n")
    mutates.reset()
    second = indexer.index_path(sample_repo, config=cfg)
    assert second.indexed == 5
    # 5 files x (delete symbols + delete file) = 10 deletes, previously 10
    # commits. Now one, and nothing else on this path mutates.
    assert mutates.count == 1


@requires_stack
def test_purged_files_share_the_same_delete_commit(git_repo, mutates):
    from witan_code import config as cfg_mod
    from witan_code import indexer

    cfg = cfg_mod.load()
    for i in range(4):
        (git_repo / f"gone{i}.py").write_text(f"def g{i}():\n    return {i}\n")
    indexer.index_path(git_repo, config=cfg)

    for i in range(4):
        (git_repo / f"gone{i}.py").unlink()
    mutates.reset()
    stats = indexer.index_path(git_repo, config=cfg)
    # purged files feed the SAME delete_steps list as reindexed ones, so the
    # whole purge is one commit rather than two per file
    assert stats.purged == 4
    assert mutates.count == 1


@requires_stack
def test_a_clean_reindex_writes_no_delete_commit(sample_repo, mutates):
    from witan_code import config as cfg_mod
    from witan_code import indexer

    cfg = cfg_mod.load()
    indexer.index_path(sample_repo, config=cfg)
    mutates.reset()
    stats = indexer.index_path(sample_repo, config=cfg)
    # nothing changed: no deletes to issue, so no mutate at all
    assert stats.indexed == 0
    assert mutates.count == 0


@requires_stack
def test_deletes_are_chunked_so_the_composed_query_stays_bounded(
    sample_repo, mutates, monkeypatch
):
    from witan_code import config as cfg_mod
    from witan_code import indexer

    # 2 delete statements per file; a cap of 4 means 2 files per commit.
    monkeypatch.setattr(indexer, "_DELETE_BATCH_SIZE", 4)
    cfg = cfg_mod.load()
    for i in range(5):
        (sample_repo / f"chunk{i}.py").write_text(f"def c{i}():\n    return {i}\n")
    indexer.index_path(sample_repo, config=cfg)

    for i in range(5):
        (sample_repo / f"chunk{i}.py").write_text(f"def c{i}():\n    return {i} + 1\n")
    mutates.reset()
    stats = indexer.index_path(sample_repo, config=cfg)
    assert stats.indexed == 5
    # 5 changed files + svc.py is unchanged -> 10 delete statements at 4 per
    # commit = 3 commits (4 + 4 + 2)
    assert mutates.count == 3
