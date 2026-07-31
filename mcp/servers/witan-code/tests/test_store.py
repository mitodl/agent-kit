"""Store stat helpers backing `code_indexed_repos` and the prompt-hook block.

Both run per store — the hook on every single prompt — so these avoid bulk
reads and per-entry Path construction. That makes their edge cases (an empty
store, an unreadable one, a dangling symlink) worth pinning explicitly.
"""

import os
from pathlib import Path

from witan_code import store as store_module

from .conftest import requires_stack


class _StubClient:
    def __init__(self, rows):
        self._rows = rows

    def read(self, *_args, **_kwargs):
        return self._rows


class _StubConfig:
    queries_dir = Path(".")


# ── file_count ────────────────────────────────────────────────────────────────


@requires_stack
def test_file_count_agrees_with_the_indexed_file_set(sample_repo):
    """Counted in the engine now, so it must still match the actual rows."""
    from witan_code import config as cfg_mod
    from witan_code import indexer
    from witan_code.graph import OmnigraphClient

    cfg = cfg_mod.load()
    indexer.index_path(sample_repo, force=False, config=cfg)
    store = store_module.store_for_repo("https://github.com/test/cg", cfg)

    rows = OmnigraphClient(str(store), cfg.queries_dir).read(
        "code_read.gq", "all_file_hashes", {}
    )
    assert len(rows) >= 1
    assert store_module.file_count(store, cfg) == len(rows)


def test_file_count_reads_the_column_positionally(tmp_path, monkeypatch):
    """count_files names its column after the match variable on a populated
    store but "?" on an empty one, so the value cannot be looked up by key."""
    monkeypatch.setattr(
        store_module, "OmnigraphClient", lambda *a, **kw: _StubClient([{"f": 990}])
    )
    assert store_module.file_count(tmp_path / "x.omni", _StubConfig()) == 990

    monkeypatch.setattr(
        store_module, "OmnigraphClient", lambda *a, **kw: _StubClient([{"?": 0}])
    )
    assert store_module.file_count(tmp_path / "x.omni", _StubConfig()) == 0


def test_file_count_is_none_when_the_store_cannot_be_read(tmp_path, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("no such store")

    monkeypatch.setattr(store_module, "OmnigraphClient", _boom)
    assert store_module.file_count(tmp_path / "x.omni", _StubConfig()) is None


# ── dir_stats ─────────────────────────────────────────────────────────────────


def test_dir_stats_sums_sizes_and_takes_the_latest_mtime(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "a.bin").write_bytes(b"x" * 10)
    (tmp_path / "nested" / "b.bin").write_bytes(b"y" * 5)
    os.utime(tmp_path / "nested" / "b.bin", (1_800_000_000, 1_800_000_000))

    total, mtime = store_module.dir_stats(tmp_path)

    assert total == 15
    assert mtime >= 1_800_000_000


def test_dir_stats_skips_a_dangling_symlink(tmp_path):
    """The rglob + is_file() form this replaced skipped these silently; os.walk
    lists them, so the stat has to be guarded or a dead link would raise."""
    (tmp_path / "real.bin").write_bytes(b"z" * 7)
    (tmp_path / "dangling").symlink_to(tmp_path / "gone.bin")

    total, _mtime = store_module.dir_stats(tmp_path)

    assert total == 7


def test_dir_stats_does_not_descend_into_symlinked_directories(tmp_path):
    """os.walk(followlinks=False) matches rglob's behavior — a store that links
    to a large tree elsewhere must not have that tree's bytes attributed to it.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"q" * 1000)

    store = tmp_path / "store"
    store.mkdir()
    (store / "own.bin").write_bytes(b"w" * 3)
    (store / "link").symlink_to(outside, target_is_directory=True)

    total, _mtime = store_module.dir_stats(store)

    assert total == 3
