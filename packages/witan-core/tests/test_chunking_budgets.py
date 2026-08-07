"""The two byte budgets bound two different hops, and must not be conflated.

`LOAD_MAX_BYTES` bounds the hop into omnigraph (a buffered request body, ~26
MiB before it refuses). `MCP_LOAD_MAX_BYTES` bounds the hop through an MCP
session, where the Python SDK rejects bodies over 4 MiB before parsing them.
Using the first where the second applies is what made `witan migrate merge`
fail against the deployment with `413 Request body too large`.
"""

import json

from mcp.server.streamable_http_manager import DEFAULT_MAX_REQUEST_BODY_SIZE

from witan_core.chunking import LOAD_MAX_BYTES, MCP_LOAD_MAX_BYTES, chunk_records

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
