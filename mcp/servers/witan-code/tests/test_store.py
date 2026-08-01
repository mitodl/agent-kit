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
    ref = store_module.StoreRef(str(tmp_path / "x.omni"))
    monkeypatch.setattr(
        store_module, "OmnigraphClient", lambda *a, **kw: _StubClient([{"f": 990}])
    )
    assert store_module.file_count(ref, _StubConfig()) == 990

    monkeypatch.setattr(
        store_module, "OmnigraphClient", lambda *a, **kw: _StubClient([{"?": 0}])
    )
    assert store_module.file_count(ref, _StubConfig()) == 0


def test_file_count_is_none_when_the_store_cannot_be_read(tmp_path, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("no such store")

    monkeypatch.setattr(store_module, "OmnigraphClient", _boom)
    ref = store_module.StoreRef(str(tmp_path / "x.omni"))
    assert store_module.file_count(ref, _StubConfig()) is None


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


# ── Cluster addressing ────────────────────────────────────────────────────────
#
# `code_server` is what turns every code graph from a directory on this machine
# into a graph on the shared omnigraph-server. These pin the three things that
# have to agree with ol-infrastructure's provisioning for that to work at all:
# the graph id, the flags it becomes, and what happens when the graph isn't
# there.


def _cluster(monkeypatch, *graphs: str, url: str = "https://omnigraph.test"):
    monkeypatch.setenv("WITAN_CODE_SERVER", url)
    monkeypatch.setattr(
        store_module, "cluster_graphs", lambda *a, **kw: frozenset(graphs)
    )
    monkeypatch.setattr(
        store_module, "safe_cluster_graphs", lambda *a, **kw: frozenset(graphs)
    )


# The exact stderr omnigraph 0.8.1 produced against the deployed CI server when
# the CLI had no usable credential (2026-08-01). Verbatim, because the whole
# point of the two exception types is telling THIS apart from a graph that
# genuinely isn't provisioned, and a paraphrase would stop testing that.
_MISSING_TOKEN_STDERR = (
    "\x1b[0mError: \n   0: \x1b[91mmissing bearer token\x1b[0m\n\n"
    "Location:\n   \x1b[35mcrates/omnigraph-cli/src/helpers.rs\x1b[0m:\x1b[35m436\x1b[0m\n"
)


def _failing_graphs_list(
    monkeypatch, stderr: str = _MISSING_TOKEN_STDERR, code: int = 1
):
    import subprocess as _sp

    monkeypatch.setattr(store_module, "_binary", lambda: "/nonexistent/omnigraph")
    monkeypatch.setattr(
        store_module.subprocess,
        "run",
        lambda *a, **kw: _sp.CompletedProcess(a[0], code, "", stderr),
    )
    store_module.reset_graph_cache()


def test_unreachable_server_is_not_reported_as_an_unprovisioned_graph(monkeypatch):
    """An auth failure and "that graph does not exist" are the same empty
    listing. Collapsing them sent the first live run to check provisioning for
    what was really a missing bearer token."""
    import pytest

    from witan_code import config as cfg_module

    monkeypatch.setenv("WITAN_CODE_SERVER", "https://omnigraph.test")
    cfg = cfg_module.load()
    _failing_graphs_list(monkeypatch)

    with pytest.raises(store_module.ClusterUnreachable) as exc:
        store_module.ensure_store("https://github.com/mitodl/agent-kit", cfg)

    message = str(exc.value)
    assert "missing bearer token" in message  # the server's own words survive
    assert "\x1b[" not in message  # ...without the ANSI codes
    assert "Location:" not in message  # ...or the Rust backtrace boilerplate
    assert "data_tier.py" not in message  # and WITHOUT blaming provisioning


def test_listings_still_degrade_when_the_server_cannot_be_asked(monkeypatch):
    """A read path has nothing better to do with an unreachable server than
    report nothing — it must not take down every `code_*` tool."""
    from witan_code import config as cfg_module

    monkeypatch.setenv("WITAN_CODE_SERVER", "https://omnigraph.test")
    cfg = cfg_module.load()
    _failing_graphs_list(monkeypatch)

    assert store_module.per_repo_stores(cfg) == []
    assert not store_module.store_for_repo("https://github.com/x/y", cfg).exists()


def test_a_failed_listing_is_not_cached(monkeypatch):
    """A transient outage must not pin an error for the whole TTL — the next
    call has to be able to succeed."""
    from witan_code import config as cfg_module

    monkeypatch.setenv("WITAN_CODE_SERVER", "https://omnigraph.test")
    cfg = cfg_module.load()
    _failing_graphs_list(monkeypatch)
    assert store_module.per_repo_stores(cfg) == []

    import subprocess as _sp

    monkeypatch.setattr(
        store_module.subprocess,
        "run",
        lambda *a, **kw: _sp.CompletedProcess(a[0], 0, '["code-github-com-x-y"]', ""),
    )
    assert [r.graph_id for r in store_module.per_repo_stores(cfg)] == [
        "code-github-com-x-y"
    ]


def test_store_for_repo_addresses_the_cluster_graph(monkeypatch):
    from witan_code import config as cfg_module

    monkeypatch.setenv("WITAN_CODE_SERVER", "https://omnigraph.test/")
    monkeypatch.setenv("WITAN_CODE_TOKEN", "tok")
    cfg = cfg_module.load()

    ref = store_module.store_for_repo("https://github.com/mitodl/ol-django", cfg)

    assert ref.is_remote
    # The trailing slash is stripped: the client splits scheme/host itself and
    # a doubled separator would reach the server as a different URL.
    assert ref.uri == "https://omnigraph.test"
    assert ref.graph_id == "code-github-com-mitodl-ol-django"
    assert ref.token == "tok"
    # The filesystem questions have an explicit answer, not a coincidental one.
    assert ref.local_path is None
    assert ref.stats() == (None, None)


def test_store_for_repo_stays_local_without_a_code_server(tmp_path, monkeypatch):
    from witan_code import config as cfg_module

    monkeypatch.delenv("WITAN_CODE_SERVER", raising=False)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path))
    cfg = cfg_module.load()

    ref = store_module.store_for_repo("https://github.com/test/cg", cfg)

    assert not ref.is_remote
    assert ref.local_path == tmp_path / "https_github.com_test_cg.omni"


def test_code_server_must_be_a_url(monkeypatch):
    """A store directory in `code_server` would resolve to `--store <path>` on
    every repo — every graph silently collapsed onto one local store."""
    import pytest

    from witan_code import config as cfg_module

    monkeypatch.setenv("WITAN_CODE_SERVER", "~/.local/share/witan/code")
    with pytest.raises(ValueError, match="http"):
        cfg_module.load()


def test_per_repo_stores_lists_the_clusters_code_graphs(monkeypatch):
    from witan_code import config as cfg_module

    _cluster(
        monkeypatch,
        "code-github-com-mitodl-ol-django",
        "code-github-com-mitodl-agent-kit",
        cfg_module.BRIDGE_GRAPH_ID,
        "council",  # witan's own memory graph, on the same server
    )
    cfg = cfg_module.load()

    ids = [r.graph_id for r in store_module.per_repo_stores(cfg)]

    # The bridge graph is listed separately (`bridge_store`) and witan's
    # memory graph is not a code graph at all.
    assert ids == [
        "code-github-com-mitodl-agent-kit",
        "code-github-com-mitodl-ol-django",
    ]


def test_ensure_store_names_the_declared_graphs_when_one_is_missing(monkeypatch):
    """The error has to say what the server DOES serve — the usual cause is a
    graph id that drifted from provisioning's, which is invisible otherwise."""
    import pytest

    from witan_code import config as cfg_module

    _cluster(monkeypatch, "code-github-com-mitodl-ol-django")
    cfg = cfg_module.load()

    with pytest.raises(store_module.ClusterGraphMissing) as exc:
        store_module.ensure_store("https://github.com/test/never-provisioned", cfg)

    message = str(exc.value)
    assert "code-github-com-test-never-provisioned" in message
    assert "code-github-com-mitodl-ol-django" in message
    assert "data_tier.py" in message


def test_ensure_store_accepts_a_declared_graph(monkeypatch):
    from witan_code import config as cfg_module

    repo = "https://github.com/mitodl/ol-django"
    _cluster(monkeypatch, cfg_module.graph_id(repo))
    cfg = cfg_module.load()

    # No init, no schema apply — provisioning declares and applies both.
    assert store_module.ensure_store(repo, cfg).graph_id == cfg_module.graph_id(repo)


def test_bridge_store_uses_the_fixed_cluster_graph_id(monkeypatch):
    from witan_code import config as cfg_module

    _cluster(monkeypatch, cfg_module.BRIDGE_GRAPH_ID)
    cfg = cfg_module.load()

    assert store_module.ensure_bridge_store(cfg).graph_id == cfg_module.BRIDGE_GRAPH_ID


def test_repo_for_store_asks_a_cluster_graph_what_it_holds(monkeypatch):
    """`graph_id` does not invert, so the URI has to come from the graph.
    A graph with no files has no answer and falls back to its id."""
    from witan_code import config as cfg_module

    monkeypatch.setenv("WITAN_CODE_SERVER", "https://omnigraph.test")
    cfg = cfg_module.load()
    ref = store_module.store_for_repo("https://github.com/mitodl/ol-django", cfg)

    monkeypatch.setattr(
        store_module,
        "OmnigraphClient",
        lambda *a, **kw: _StubClient(
            [{"f.repo": "https://github.com/mitodl/ol-django"}]
        ),
    )
    assert (
        store_module.repo_for_store(ref, cfg) == "https://github.com/mitodl/ol-django"
    )

    monkeypatch.setattr(
        store_module, "OmnigraphClient", lambda *a, **kw: _StubClient([])
    )
    assert store_module.repo_for_store(ref, cfg) == "code-github-com-mitodl-ol-django"


def test_parse_graph_ids_reads_both_envelopes():
    assert store_module._parse_graph_ids('["a", "b"]') == ["a", "b"]
    assert store_module._parse_graph_ids('{"graphs": [{"id": "a"}]}') == ["a"]
    assert store_module._parse_graph_ids('{"graphs": [{"name": "b"}]}') == ["b"]
    assert store_module._parse_graph_ids("not json") == []


def test_parse_graph_ids_mixes_plain_ids_and_records_without_calling_get_on_a_str():
    """A plain-string row must never reach `.get`.

    The row-shape expression reads as though the `.get`s might be evaluated
    unconditionally (a review read it that way). They are not — `or` binds
    tighter than the conditional — and a mixed list is the case that would
    raise AttributeError if that were ever to change.
    """
    assert store_module._parse_graph_ids('["a", {"id": "b"}]') == ["a", "b"]
    # Non-string, non-dict rows are skipped rather than coerced.
    assert store_module._parse_graph_ids('["a", 7, null, {"id": "b"}]') == ["a", "b"]
