"""Splitting a bulk load into batches omnigraph-server will actually accept.

``omnigraph load`` POSTs its whole data file as one request body, and the
server buffers it. Above a cap it answers ``413 Payload Too Large: Failed to
buffer the request body``, which is how a repo-scale index dies against a
deployed server while working fine against a local store.

The cap is not reachable from configuration on either side: it is an axum
``DefaultBodyLimit``, and neither ``omnigraph-server --help`` nor ``omnigraph
load --help`` exposes a knob for it. (It is NOT
``OMNIGRAPH_PER_ACTOR_BYTES_MAX``, which is 256 MiB in the deployed cluster —
a per-actor admission budget the 413 fires an order of magnitude below.) So
the split has to happen client-side.

Shared rather than witan-code's own since 2026-08-06: witan's ``migrate merge``
ships a personal graph's rows through the MCP tier the same way witan-code
ships an index (witan ADR-0007 D5), and hits the same two ceilings — the
buffered body one layer down, and the records riding as a JSON tool parameter.
One rule, one place.

★ THOSE TWO CEILINGS ARE NOT THE SAME SIZE, which is why there are two budgets
here. ``LOAD_MAX_BYTES`` (8 MiB) bounds the hop into omnigraph itself.
``MCP_LOAD_MAX_BYTES`` (2 MiB) bounds the hop through an MCP session, where the
Python SDK caps request bodies at 4 MiB. A caller that reaches the store by
shelling out wants the first; a caller that reaches it through a ``*_store_*``
tool wants the second. Using one budget for both is not a tuning mistake, it is
a correctness one — it shipped that way, and a real ``migrate merge`` against
the deployment failed with ``413 Request body too large`` on its first call.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

__all__ = [
    "LOAD_MAX_BYTES",
    "MCP_LOAD_MAX_BYTES",
    "chunk_records",
    "describe_budget",
]

# Bisected against omnigraph 0.8.1 — the version deployed in the cluster:
# ~26 MiB of records accepted, ~54 MiB refused. The cap is only known as that
# range, so aim well under its low end rather than close to it.
LOAD_MAX_BYTES = 8 * 1024 * 1024

# The ceiling for records riding as a JSON tool parameter over an MCP session,
# which is a DIFFERENT hop from the one LOAD_MAX_BYTES bounds and an order of
# magnitude tighter. Measured live against the CI deployment 2026-08-07: the
# MCP Python SDK caps Streamable HTTP request bodies at
# ``mcp.server.streamable_http_manager.DEFAULT_MAX_REQUEST_BODY_SIZE`` — 4 MiB
# — in ASGI middleware ahead of parsing, answering ``413 Request body too
# large``. FastMCP exposes no way to raise it (its session-manager subclass
# neither accepts nor forwards ``max_request_body_size``), so the client has to
# stay under it.
#
# NOT set to the cap, for two reasons that both bite:
#  1. `_batches` counts JSONL framing (one `json.dumps` + a newline per record)
#     while the wire carries a JSON-RPC envelope with the records as an array —
#     measured at ~1.03x the JSONL bytes. A budget set AT 4 MiB overflows.
#  2. The cap belongs to a deployment this client does not control and cannot
#     interrogate. Leaving real headroom is what keeps a server-side default
#     change from becoming our outage.
# 2 MiB costs a 5.4 MiB personal graph 4 requests instead of 2 — worth it.
# `test_mcp_bound_stays_clear_of_the_sdk_cap` pins the relationship to the SDK
# constant so an SDK bump that lowers the cap fails CI instead of production.
MCP_LOAD_MAX_BYTES = 2 * 1024 * 1024


def describe_budget(max_bytes: int) -> str:
    """A byte budget as a phrase for an error message, exact at any size.

    Says "2 MiB" for the constants above and falls back to an exact byte count
    for anything else, because the budget is not always one of them: ``load``
    takes ``max_bytes`` from its caller. An error that rounds a 1,500,000-byte
    budget to "1 MiB" tells the reader to look for a limit that is not the one
    that refused them.
    """
    mib = 1024 * 1024
    return f"{max_bytes // mib} MiB" if max_bytes % mib == 0 else f"{max_bytes:,} bytes"


def chunk_records(
    records: Iterable[dict],
    max_bytes: int = LOAD_MAX_BYTES,
) -> Iterator[list[dict]]:
    """Yield byte-bounded batches of load records, every node before any edge.

    ORDER IS LOAD-BEARING HERE, and not for the reason ``change_many``'s is.
    Measured against 0.8.1: an edge resolves against nodes already persisted by
    an earlier load, OR against nodes anywhere in the same batch (position
    within a batch does not matter) — but an endpoint in neither fails the
    WHOLE load with ``dst '...' not found in <Node>``.

    That rules out slicing the record list as it stands. ``indexer`` builds it
    per file as ``[the file's nodes, the file's edges]`` and concatenates, while
    Calls/References/Imports/Inherits edges routinely point at symbols defined
    in files appearing LATER in the list. Chunking by index would put those
    edges in a batch before their target nodes and break runs that work today —
    unpredictably, since it depends on which repo and which file order. Emitting
    every node first instead makes the resolvable set identical to the
    single-call load this replaces.

    A record larger than ``max_bytes`` on its own is still yielded, alone: it
    cannot be split, and refusing it here would only trade a server-side 413 for
    a client-side error.
    """
    if max_bytes < 1:
        msg = f"max_bytes must be >= 1, got {max_bytes}"
        raise ValueError(msg)
    # Partitioned in ONE pass: a repo-scale index passes hundreds of thousands
    # of records, and materializing the input before splitting it held two full
    # pointer lists at once for no benefit.
    nodes: list[dict] = []
    edges: list[dict] = []
    for record in records:
        (nodes if "type" in record else edges).append(record)
    for group in (nodes, edges):
        yield from _batches(group, max_bytes)


def _batches(records: list[dict], max_bytes: int) -> Iterator[list[dict]]:
    batch: list[dict] = []
    size = 0
    for record in records:
        # +1 for the newline the JSONL writer adds after each record.
        cost = len(json.dumps(record).encode()) + 1
        if batch and size + cost > max_bytes:
            yield batch
            batch, size = [], 0
        batch.append(record)
        size += cost
    if batch:
        yield batch
