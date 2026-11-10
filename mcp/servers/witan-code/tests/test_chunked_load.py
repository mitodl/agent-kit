"""A repo-scale load must survive the server's request-body ceiling.

`omnigraph load` POSTs its data file as one body, so a large repo got back
`413 Payload Too Large` against the deployed server while working fine against
a local store. Splitting it is not just slicing: an edge whose endpoint is in
neither the store nor its own batch fails the WHOLE load, and the indexer's
record list routinely has cross-file edges ahead of their target nodes.
"""

import hashlib
import json

import pytest

from witan_code import indexer
from witan_core.chunking import LOAD_MAX_BYTES, chunk_records

from .conftest import requires_stack


def _node(slug, type_="Symbol", **extra):
    return {"type": type_, "data": {"slug": slug, **extra}}


def _edge(kind, src, dst):
    return {"edge": kind, "from": src, "to": dst}


# ── chunk_records ─────────────────────────────────────────────────


def test_empty_yields_nothing():
    assert list(chunk_records([])) == []


def test_max_bytes_must_be_positive():
    with pytest.raises(ValueError, match="max_bytes must be >= 1"):
        list(chunk_records([_node("a")], 0))


def test_every_node_precedes_every_edge():
    """The property the whole design rests on.

    Input deliberately interleaves them the way `_file_records` does — each
    file's nodes then its edges — with an edge pointing at a node defined in a
    LATER file. Slicing by index would put that edge in an earlier batch than
    its target and fail the load.
    """
    records = [
        _node("f1::a"),
        _edge("Calls", "f1::a", "f2::b"),  # target defined below
        _node("f2::b"),
        _edge("Calls", "f2::b", "f1::a"),
    ]
    batches = list(chunk_records(records, LOAD_MAX_BYTES))
    flat = [record for batch in batches for record in batch]
    last_node = max(i for i, r in enumerate(flat) if "type" in r)
    first_edge = min(i for i, r in enumerate(flat) if "type" not in r)
    assert last_node < first_edge
    assert len(flat) == len(records), "no record may be dropped"


def test_nothing_is_dropped_or_duplicated():
    records = [_node(f"s{i}") for i in range(50)]
    records += [_edge("Calls", f"s{i}", f"s{i + 1}") for i in range(49)]
    flat = [r for batch in chunk_records(records, 200) for r in batch]
    assert flat == [r for r in records if "type" in r] + [
        r for r in records if "type" not in r
    ]


def test_batches_respect_the_byte_budget():
    records = [_node(f"s{i}", padding="x" * 100) for i in range(40)]
    budget = 500
    batches = list(chunk_records(records, budget))
    assert len(batches) > 1, "the budget must actually force a split"
    for batch in batches:
        if len(batch) == 1:
            continue  # an unsplittable single record is allowed to exceed
        size = sum(len(json.dumps(r).encode()) + 1 for r in batch)
        assert size <= budget


def test_an_oversized_record_is_yielded_alone_not_dropped():
    big = _node("huge", padding="x" * 5000)
    records = [_node("small"), big, _node("small2")]
    batches = list(chunk_records(records, 100))
    flat = [r for batch in batches for r in batch]
    assert big in flat
    assert [b for b in batches if b == [big]], "oversized record needs its own batch"


# ── _defer_content_hashes ─────────────────────────────────────────


def test_defer_swaps_in_a_sentinel_and_returns_the_real_rows():
    real_hash = hashlib.sha256(b"x").hexdigest()
    records = [
        {"type": "CodeFile", "data": {"slug": "r#a.py", "content_hash": real_hash}},
        _node("r#a.py::foo"),
        _edge("Defines", "r#a.py", "r#a.py::foo"),
    ]
    returned = indexer._defer_content_hashes(records)

    assert records[0]["data"]["content_hash"] == indexer._PENDING_CONTENT_HASH
    assert returned == [
        {"type": "CodeFile", "data": {"slug": "r#a.py", "content_hash": real_hash}}
    ]
    assert records[1:] == [
        _node("r#a.py::foo"),
        _edge("Defines", "r#a.py", "r#a.py::foo"),
    ]


def test_one_batch_through_omnigraph_needs_no_deferred_hashes(monkeypatch):
    records = [_node("a"), _edge("Calls", "a", "b"), _node("b")]
    client = object()
    assert not indexer._hashes_need_deferring(client, records)

    one_record = len(json.dumps(records[0]).encode()) + 1
    monkeypatch.setattr(indexer, "_ONE_LOAD_MAX_BYTES", one_record)
    assert indexer._hashes_need_deferring(client, records)


def test_the_one_load_budget_is_no_larger_than_omnigraph_clients_chunk_at():
    """`_hashes_need_deferring` calls a load one commit when it fits this
    budget, which holds only while the client splits at no less."""
    import inspect

    from witan_code.graph import OmnigraphClient

    default = inspect.signature(OmnigraphClient.load).parameters["max_bytes"].default
    assert default >= indexer._ONE_LOAD_MAX_BYTES


def test_a_remote_store_client_always_defers_hashes():
    """The store tier re-chunks a received load, and an older one splits
    nodes from edges, so one client-side batch is not one commit there."""
    from witan_code.remote.store import RemoteStoreClient, StoreSession

    session = StoreSession("http://x/mcp", lambda: "jwt", lambda _t: None)
    client = RemoteStoreClient("https://github.com/x/y", session)
    assert indexer._hashes_need_deferring(client, [_node("a")])


def test_sentinel_can_never_collide_with_a_real_hash():
    """The skip check is `stored == sha256(file)`; the sentinel must never match."""
    digest = hashlib.sha256(b"anything").hexdigest()
    assert indexer._PENDING_CONTENT_HASH != digest
    assert len(indexer._PENDING_CONTENT_HASH) != len(digest)


# ── Against a real store ──────────────────────────────────────────


@requires_stack
def test_chunked_load_builds_the_same_graph_as_one_call(sample_repo, monkeypatch):
    """Force many tiny batches and prove cross-batch edges still resolve.

    This is the test that would have caught naive slicing: with a 1-record
    budget, every edge is in its own batch and can only resolve against nodes
    already persisted by an earlier one.
    """
    from witan_code import config as cfg_mod
    from witan_code import graph as graph_mod

    real_load = graph_mod.OmnigraphClient.load

    def tiny_batches(self, records, mode="merge", **kwargs):
        return real_load(self, records, mode, max_bytes=1)

    monkeypatch.setattr(graph_mod.OmnigraphClient, "load", tiny_batches)
    stats = indexer.index_path(sample_repo, config=cfg_mod.load())
    assert stats.indexed >= 1
    assert stats.edges >= 1

    from witan_code import repo as repo_mod
    from witan_code import store as store_mod

    cfg = cfg_mod.load()
    client = store_mod.ensure_store(repo_mod.detect(start=sample_repo), cfg).client(cfg)
    hashes = {
        row["slug"]: row["content_hash"]
        for row in client.read("code_read.gq", "all_file_hashes", {})
    }
    assert hashes, "the repo should have indexed files"
    assert indexer._PENDING_CONTENT_HASH not in hashes.values(), (
        "every hash must be committed once the load completed"
    )


@requires_stack
def test_a_failed_load_leaves_the_file_reindexable(sample_repo, monkeypatch):
    """The property the sentinel exists to preserve.

    If the hash-commit load never happens, the next run must re-index the file
    rather than skip it. Without the sentinel the file would carry its real
    hash from the first load and be skipped forever.
    """
    from witan_code import config as cfg_mod
    from witan_code import graph as graph_mod

    # The sample repo fits one batch, which loads hashes and all in one atomic
    # commit; a budget of one byte puts it on the deferred two-load path.
    monkeypatch.setattr(indexer, "_ONE_LOAD_MAX_BYTES", 1)
    real_load = graph_mod.OmnigraphClient.load
    calls = {"n": 0}

    def fail_the_hash_commit(self, records, mode="merge", **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # the hash-commit load
            msg = "simulated mid-load failure"
            raise RuntimeError(msg)
        return real_load(self, records, mode, **kwargs)

    monkeypatch.setattr(graph_mod.OmnigraphClient, "load", fail_the_hash_commit)
    cfg = cfg_mod.load()
    with pytest.raises(RuntimeError, match="simulated mid-load failure"):
        indexer.index_path(sample_repo, config=cfg)

    monkeypatch.setattr(graph_mod.OmnigraphClient, "load", real_load)
    stats = indexer.index_path(sample_repo, config=cfg_mod.load())
    assert stats.indexed >= 1, "the file must be re-indexed, not skipped"
    assert stats.skipped == 0


@requires_stack
def test_a_load_that_fits_one_batch_commits_hashes_with_it(sample_repo, monkeypatch):
    """No sentinel and no second load when the whole write is one batch: the
    hash lands in the same atomic commit as the file's symbols and edges."""
    from witan_code import config as cfg_mod
    from witan_code import graph as graph_mod

    real_load = graph_mod.OmnigraphClient.load
    loaded: list[list[dict]] = []

    def _recorded(self, records, mode="merge", **kwargs):
        loaded.append(records)
        return real_load(self, records, mode, **kwargs)

    monkeypatch.setattr(graph_mod.OmnigraphClient, "load", _recorded)
    indexer.index_path(sample_repo, config=cfg_mod.load())

    code_loads = [r for r in loaded if any(x.get("type") == "CodeFile" for x in r)]
    assert len(code_loads) == 1
    hashes = [
        x["data"]["content_hash"] for x in code_loads[0] if x.get("type") == "CodeFile"
    ]
    assert hashes and indexer._PENDING_CONTENT_HASH not in hashes


@requires_stack
def test_a_failed_single_load_leaves_the_file_reindexable(sample_repo, monkeypatch):
    """The sentinel's guarantee holds without it when the load is one commit:
    a failed load writes no CodeFile row, so there is no hash to skip on."""
    from witan_code import config as cfg_mod
    from witan_code import graph as graph_mod

    real_load = graph_mod.OmnigraphClient.load

    def _fail(self, records, mode="merge", **kwargs):
        msg = "simulated load failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(graph_mod.OmnigraphClient, "load", _fail)
    with pytest.raises(RuntimeError, match="simulated load failure"):
        indexer.index_path(sample_repo, config=cfg_mod.load())

    monkeypatch.setattr(graph_mod.OmnigraphClient, "load", real_load)
    stats = indexer.index_path(sample_repo, config=cfg_mod.load())
    assert stats.indexed >= 1, "the file must be re-indexed, not skipped"
    assert stats.skipped == 0
