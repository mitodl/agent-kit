"""`witan migrate merge` must answer "did all of it arrive?" by itself.

The documented verification was `omnigraph export` on both stores plus a `jq`
type-count comparison. Once the target is the deployed graph that is
unavailable to the person who just migrated: the data tier is ClusterIP-only
and they hold no bearer token for it. So the merge reports its own accounting,
and these tests hold it to the two identities in `witan.merge_report` — that
every source row reached a decision, and that every decided row was written.
"""

from __future__ import annotations

import json

import pytest

from .conftest import _Tools, requires_omnigraph
from .test_migrate import _init_store, _insert_memory


@pytest.fixture
def proxy(server, monkeypatch):
    """A real `RemoteServerProxy` speaking to the in-process FastMCP server.

    The same fixture `test_remote_proxy` uses, and a real proxy rather than a
    stub for the same reason: the accounting is summed across batches by the
    client and reported per batch by the server, so a fake on either side would
    verify one half against an assumption about the other.
    """
    from fastmcp import Client
    from witan.config import RemoteConfig
    from witan.remote.proxy import RemoteServerProxy

    import witan.server as srv

    cfg = RemoteConfig(url="http://unused/mcp", oidc_issuer="https://sso/realms/ol")
    p = RemoteServerProxy(cfg, lambda: "tok")
    monkeypatch.setattr(p, "_new_client", lambda _token: Client(srv.mcp))
    return p


# ── the arithmetic, in isolation ──────────────────────────────────────────


def test_accounting_reconciles_a_complete_merge():
    from witan import merge_report

    check = merge_report.accounting(
        {
            "source_rows": 10,
            "added": 4,
            "updated": 2,
            "kept_target": 1,
            "passthrough": 3,
            "duplicate_slugs": 0,
            "rows_loaded": 9,
        }
    )

    assert check["complete"] is True
    assert (check["decided"], check["undecided"]) == (10, 0)
    # kept_target writes nothing, so 9 of the 10 were expected to land.
    assert (check["rows_loaded"], check["expected_loaded"], check["unwritten"]) == (
        9,
        9,
        0,
    )


def test_a_source_row_that_never_reached_a_decision_is_a_shortfall():
    """The deployed transport's failure shape: a batch that errors returns no
    decisions at all, so its rows are in `source_rows` and in no bucket."""
    from witan import merge_report

    check = merge_report.accounting(
        {
            "source_rows": 10,
            "added": 4,
            "updated": 0,
            "kept_target": 0,
            "passthrough": 0,
            "duplicate_slugs": 0,
            "rows_loaded": 4,
        }
    )

    assert check["complete"] is False
    assert check["undecided"] == 6


def test_a_decided_row_that_was_never_written_is_a_shortfall():
    """The in-process transport's failure shape, and the reason both identities
    exist. Reconciliation runs over the whole export before the first load, so
    every row IS decided; only the written check sees the load that died."""
    from witan import merge_report

    check = merge_report.accounting(
        {
            "source_rows": 10,
            "added": 7,
            "updated": 0,
            "kept_target": 3,
            "passthrough": 0,
            "duplicate_slugs": 0,
            "rows_loaded": 2,
        }
    )

    assert check["undecided"] == 0, "the decided identity cannot see this failure"
    assert check["complete"] is False
    assert check["unwritten"] == 5


def test_duplicate_slugs_are_accounted_rather_than_read_as_missing():
    """A hand-assembled source may repeat a `(type, slug)`; the classifier
    collapses it. Counting the collapse is what keeps a legitimate source from
    reporting a shortfall — a check that cries wolf gets ignored."""
    from witan import merge_report

    check = merge_report.accounting(
        {
            "source_rows": 5,
            "added": 3,
            "updated": 0,
            "kept_target": 0,
            "passthrough": 0,
            "duplicate_slugs": 2,
            "rows_loaded": 3,
        }
    )

    assert check["complete"] is True


def test_a_dry_run_is_held_only_to_the_decided_identity():
    """It writes nothing on purpose. Checking it against the written identity
    would report every plan as a failed merge."""
    from witan import merge_report

    check = merge_report.accounting(
        {
            "dry_run": True,
            "source_rows": 4,
            "added": 4,
            "updated": 0,
            "kept_target": 0,
            "passthrough": 0,
            "duplicate_slugs": 0,
            "rows_loaded": 0,
        }
    )

    assert check["complete"] is True
    assert check["unwritten"] is None


def test_a_result_without_the_fields_reports_cannot_tell_not_zero():
    """A deployment older than these fields. Defaulting the missing counts
    would report every edge row in the source as unaccounted for — a merge
    failure that did not happen."""
    from witan import merge_report

    assert merge_report.accounting({"added": 3, "rows_loaded": 3}) is None


def test_attaching_a_partial_never_overwrites_an_inner_one():
    """The proxy's batch loop wraps a call that may already have attached its
    own, and the innermost knows how far the merge really got."""
    from witan import merge_report

    exc = RuntimeError("boom")
    merge_report.attach_partial(exc, {"batches_applied": 3})
    merge_report.attach_partial(exc, {"batches_applied": 0})

    assert merge_report.partial_of(exc) == {"batches_applied": 3}


def test_partial_of_an_untouched_exception_is_none():
    from witan import merge_report

    assert merge_report.partial_of(RuntimeError("boom")) is None


# ── the identity against real merges ──────────────────────────────────────


def _memory_with_a_topic_edge(client, slug):
    """A node plus an edge, so `passthrough` is exercised rather than assumed.

    An export with no edge rows balances even if `passthrough` were dropped
    entirely, which is exactly the bug this accounting exists to catch.
    """
    _insert_memory(
        client, slug=slug, content="has topics", updated_at="2026-01-01T00:00:00Z"
    )
    client.change(
        "mutations.gq",
        "insert_topic",
        {
            "slug": "tp-topic-accounting",
            "name": "accounting",
            "kind": "topic",
            "created_at": "2026-01-01T00:00:00Z",
        },
    )
    client.change(
        "mutations.gq",
        "link_tagged",
        {"from": slug, "to": "tp-topic-accounting"},
    )


@requires_omnigraph
def test_an_in_process_merge_accounts_for_every_source_row(server, tmp_path):
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import merge_report
    from witan import server as srv

    source = graph_mod.OmnigraphClient(
        _init_store(tmp_path / "mine.omni"), cfg_mod.load().queries_dir
    )
    _memory_with_a_topic_edge(source, "mem-accounted-inproc-a1b2c3")

    result = srv.merge_store(source.graph_uri)

    check = merge_report.accounting(result)
    assert check is not None, "the in-process path must report accounting too"
    assert check["complete"] is True, check
    # The source really did carry unreconcilable rows, so `passthrough` is
    # load-bearing in that sum rather than a zero that happens to balance.
    assert check["breakdown"]["passthrough"] > 0
    assert check["source_rows"] == check["decided"]


@requires_omnigraph
def test_a_remote_merge_accounts_for_every_source_row(proxy, server, tmp_path):
    """Acceptance 1: the user who cannot export the target still gets an
    answer, summed across however many batches the merge became."""
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import merge_report

    source = graph_mod.OmnigraphClient(
        _init_store(tmp_path / "mine.omni"), cfg_mod.load().queries_dir
    )
    _memory_with_a_topic_edge(source, "mem-accounted-remote-d4e5f6")

    result = proxy.merge_store(source.graph_uri)

    check = merge_report.accounting(result)
    assert check is not None
    assert check["complete"] is True, check
    assert check["breakdown"]["passthrough"] > 0
    assert result["batches"] == result["batches_applied"]


@requires_omnigraph
def test_a_remote_merge_interrupted_mid_batch_reports_the_shortfall(
    proxy, server, tmp_path, monkeypatch
):
    """Acceptance 2: interrupt a merge and the report is visibly incomplete.

    A `KeyboardInterrupt` specifically, because that is what "interrupt it"
    means at a terminal and because `BaseException` is what the batch loop has
    to catch for the totals to survive it — an `except Exception` would let the
    one failure a person causes deliberately escape without a report.
    """
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import merge_report

    source = graph_mod.OmnigraphClient(
        _init_store(tmp_path / "mine.omni"), cfg_mod.load().queries_dir
    )
    for n in range(4):
        _insert_memory(
            source,
            slug=f"mem-interrupted-{n:02d}aaaa",
            content=f"row {n}",
            updated_at="2026-01-01T00:00:00Z",
        )

    # One row per batch, so there is a "part way" to stop at.
    monkeypatch.setattr("witan.remote.proxy.MCP_LOAD_MAX_BYTES", 1)
    real = proxy.store_merge
    calls = {"n": 0}

    def interrupt_on_the_third(**kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt
        return real(**kwargs)

    monkeypatch.setattr(proxy, "store_merge", interrupt_on_the_third)

    with pytest.raises(KeyboardInterrupt) as caught:
        proxy.merge_store(source.graph_uri)

    partial = merge_report.partial_of(caught.value)
    assert partial is not None, "the interrupt must carry how far the merge got"
    assert partial["batches_applied"] == 2

    check = merge_report.accounting(partial)
    assert check["complete"] is False
    # 4 source rows, 2 batches of 1 applied: the other 2 never reached a
    # decision. That is the number the report shows, and the whole point —
    # without it the interrupt says only that it was interrupted.
    assert check["source_rows"] == 4
    assert check["undecided"] == 2


@requires_omnigraph
def test_an_in_process_merge_whose_load_dies_reports_rows_it_never_wrote(
    server, tmp_path, monkeypatch
):
    """The other transport's half-merge. Reconciliation completed, so only the
    written identity can see this — the decided one balances perfectly."""
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import merge_report
    from witan import server as srv

    source = graph_mod.OmnigraphClient(
        _init_store(tmp_path / "mine.omni"), cfg_mod.load().queries_dir
    )
    for n in range(4):
        _insert_memory(
            source,
            slug=f"mem-load-dies-{n:02d}aaaa",
            content=f"row {n}",
            updated_at="2026-01-01T00:00:00Z",
        )

    monkeypatch.setattr(
        srv.chunking,
        "chunk_records",
        lambda records, *a, **kw: [[r] for r in records],
    )
    real_load = type(srv.client).load_batch
    calls = {"n": 0}

    def die_on_the_third(self, records, mode="merge"):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("data tier went away")
        return real_load(self, records, mode)

    monkeypatch.setattr(type(srv.client), "load_batch", die_on_the_third)

    with pytest.raises(RuntimeError) as caught:
        srv.merge_store(source.graph_uri)

    partial = merge_report.partial_of(caught.value)
    assert partial is not None
    check = merge_report.accounting(partial)
    assert check["undecided"] == 0, "reconciliation completed, so nothing is undecided"
    assert check["complete"] is False
    assert check["unwritten"] == 2, check


# ── the classifier's own third value ──────────────────────────────────────


def test_the_classifier_counts_rows_it_collapses(tmp_path):
    from witan import server as srv

    rows = [
        {"type": "Memory", "data": {"slug": "mem-dup-aaaaaa", "content": "first"}},
        {"type": "Memory", "data": {"slug": "mem-dup-aaaaaa", "content": "second"}},
        {"edge": "Tagged", "from": "mem-dup-aaaaaa", "to": "tp-x", "data": {}},
    ]
    export = tmp_path / "export.jsonl"
    export.write_text("".join(json.dumps(r) + "\n" for r in rows))

    nodes, passthrough, duplicates = srv._classify_rows(rows, "merge batch")

    assert (len(nodes), len(passthrough), duplicates) == (1, 1, 1)
    # WHICH row survives is documented, so it is asserted: the dict assignment
    # means the LAST row wins and the earlier one is what the count counts.
    # The docs said this backwards until review caught it, and a
    # hand-assembled source's author needs to know which record reconciles.
    assert nodes[("Memory", "mem-dup-aaaaaa")]["data"]["content"] == "second"
    # Still the single classifier for both transports: the wire path and the
    # file path must agree on all three values, not just the first two.
    assert srv._classify_rows(rows, "merge batch") == srv._parse_export(export)
    # And the three sum back to what was handed in, which is the identity the
    # whole report rests on.
    assert len(nodes) + len(passthrough) + duplicates == len(rows)


def test_store_merge_reports_its_batch_share_of_the_accounting(server):
    """The MCP tool's own numbers, so a direct caller can check one batch."""
    from witan import server as srv

    tools = _Tools(srv)
    result = tools.store_merge(
        rows=[
            {
                "type": "Memory",
                "data": {"slug": "mem-batch-share-a1b2c3", "content": "x"},
            },
            {"edge": "Tagged", "from": "mem-batch-share-a1b2c3", "to": "tp-x"},
        ],
        dry_run=True,
    )

    assert result["source_rows"] == 2
    assert result["passthrough"] == 1
    assert result["duplicate_slugs"] == 0


def test_an_empty_batch_still_reports_a_balanced_accounting(server):
    from witan import merge_report
    from witan import server as srv

    result = _Tools(srv).store_merge(rows=[])

    assert merge_report.accounting(result)["complete"] is True
