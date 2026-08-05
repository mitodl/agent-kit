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
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

__all__ = ["LOAD_MAX_BYTES", "chunk_records"]

# Bisected against omnigraph 0.8.1 — the version deployed in the cluster:
# ~26 MiB of records accepted, ~54 MiB refused. The cap is only known as that
# range, so aim well under its low end rather than close to it.
LOAD_MAX_BYTES = 8 * 1024 * 1024


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
    materialized = list(records)
    nodes = [record for record in materialized if "type" in record]
    edges = [record for record in materialized if "type" not in record]
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
