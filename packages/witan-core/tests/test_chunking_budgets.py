"""The two byte budgets bound two different hops, and must not be conflated.

`LOAD_MAX_BYTES` bounds the hop into omnigraph (a buffered request body, ~26
MiB before it refuses). `MCP_LOAD_MAX_BYTES` bounds the hop through an MCP
session, where the Python SDK rejects bodies over 4 MiB before parsing them.
Using the first where the second applies is what made `witan migrate merge`
fail against the deployment with `413 Request body too large`.
"""

import json

from mcp.server.streamable_http_manager import DEFAULT_MAX_REQUEST_BODY_SIZE
from witan_core.chunking import (
    LOAD_MAX_BYTES,
    LOAD_MAX_ROWS,
    MCP_LOAD_MAX_BYTES,
    chunk_records,
)

# omnigraph >= 0.9.0 refuses a keyed write staging more than this many rows in
# one table. Measured directly against the 0.9.0 binary on a local store:
# 8,192 rows load; 8,193 fail with "resource limit exceeded for keyed rows for
# node:Memory: actual 8193, limit 8192".
OMNIGRAPH_KEYED_ROW_CAP = 8192

# What the JSON-RPC envelope adds over the JSONL framing `_batches` counts.
# Measured at ~1.03x against a real 5.4 MiB store export; 1.10 is deliberate
# slack so this asserts a *comfortable* margin, not a marginal one.
ENVELOPE_OVERHEAD = 1.10


def test_mcp_bound_stays_clear_of_the_sdk_cap():
    """The point of the whole change: our budget must fit inside the SDK's.

    Asserted against the SDK constant itself rather than a copied 4 MiB, so an
    upgrade that lowers the cap fails here instead of in production — which is
    exactly how the original mismatch escaped every local test.
    """
    assert MCP_LOAD_MAX_BYTES * ENVELOPE_OVERHEAD < DEFAULT_MAX_REQUEST_BODY_SIZE


def test_the_omnigraph_bound_would_not_fit_the_sdk_cap():
    """Pins WHY a second constant exists, so the two don't get merged back.

    If this ever stops holding, the SDK cap has grown past omnigraph's budget
    and MCP_LOAD_MAX_BYTES has no reason to exist — delete it deliberately
    rather than letting the distinction rot.
    """
    assert LOAD_MAX_BYTES > DEFAULT_MAX_REQUEST_BODY_SIZE


def test_batches_under_the_mcp_bound_fit_a_real_request_body():
    """A packed batch must survive re-serialisation as MCP tool arguments.

    `_batches` measures JSONL bytes; the wire carries the records as a JSON
    array inside a JSON-RPC envelope. This builds that actual body and checks
    it against the SDK cap, so the accounting difference cannot silently eat
    the margin.
    """
    # Records sized so several batches are needed at the MCP bound.
    records = [
        {"type": "Memory", "data": {"slug": f"mem-{i}", "content": "x" * 4096}}
        for i in range(1200)
    ]

    batches = list(chunk_records(records, MCP_LOAD_MAX_BYTES))
    assert len(batches) > 1, "test needs a multi-batch case to be meaningful"

    for batch in batches:
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "store_merge",
                    "arguments": {"rows": batch, "dry_run": False},
                },
            }
        )
        assert len(body.encode()) < DEFAULT_MAX_REQUEST_BODY_SIZE


def test_row_bound_stays_under_the_engine_cap():
    """Same shape as the MCP-vs-SDK assertion above: our budget must fit
    inside the limit it exists to respect, with room to spare."""
    assert LOAD_MAX_ROWS < OMNIGRAPH_KEYED_ROW_CAP


def test_many_small_rows_are_split_by_count_not_bytes():
    """The regression 0.9.0 introduced. 20,000 ~230-byte Memory rows are about
    4.5 MiB — inside LOAD_MAX_BYTES — so byte-only chunking emitted ONE batch
    and omnigraph refused it at 8,193 rows."""
    records = [
        {
            "type": "Memory",
            "data": {"slug": f"mem-{i:06d}", "content": f"body for row {i}"},
        }
        for i in range(20_000)
    ]

    total = sum(len(json.dumps(r).encode()) + 1 for r in records)
    assert total < LOAD_MAX_BYTES, "test is meaningless if bytes alone would split"

    batches = list(chunk_records(records))

    assert len(batches) > 1
    for batch in batches:
        assert len(batch) <= OMNIGRAPH_KEYED_ROW_CAP
    assert sum(len(b) for b in batches) == len(records)


def test_the_row_cap_is_counted_per_table_not_per_batch():
    """omnigraph names the table in its refusal ("keyed rows for node:Memory"),
    so the cap is per type. Counting a batch as a whole would split loads that
    omnigraph would have accepted whole."""
    records = [
        {"type": "Memory" if i % 2 else "Task", "data": {"slug": f"row-{i}"}}
        for i in range(2 * LOAD_MAX_ROWS)
    ]

    batches = list(chunk_records(records))

    # LOAD_MAX_ROWS of each type — every row fits in one batch on a per-table
    # count, and would have needed two on a per-batch one.
    assert len(batches) == 1
    assert len(batches[0]) == 2 * LOAD_MAX_ROWS


def test_nodes_still_precede_edges_when_the_row_cap_splits():
    """The row bound must not disturb the node/edge ordering that makes edge
    endpoints resolvable — an edge in a batch before its target node fails the
    whole load with "dst '...' not found"."""
    records = [{"type": "Memory", "data": {"slug": f"m-{i}"}} for i in range(9_000)]
    records += [
        {"edge": "RelatesTo", "from": f"m-{i}", "to": f"m-{i + 1}"}
        for i in range(9_000)
    ]

    batches = list(chunk_records(records))

    seen_edge = False
    for batch in batches:
        for record in batch:
            if "edge" in record:
                seen_edge = True
            else:
                assert not seen_edge, "a node followed an edge across batches"
    assert seen_edge
