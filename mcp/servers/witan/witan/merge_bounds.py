"""How many tasks one fenced merge write, and one ``store_merge`` call, may carry.

A merge writes a winning Task row the target already holds as a ``merge_task``
statement in a composed mutation fenced on a fresh read (see
``witan.server._write_task_batch``), not as part of a load. Three ceilings
bound that mutation, measured against omnigraph 0.13.0 on 2026-10-19 with
2,000 tasks in the graph and ~800 bytes of prose per row:

- Time. One fenced commit of N statements took, median of three, 0.61s at 32,
  1.27s at 64, 2.68s at 128 and 6.68s at 256 against a local omnigraph-server
  over HTTP (19-26ms a statement); CLI-direct on a local store, 0.77s at 32 and
  1.13s at 64. That is a local disk. The deployment is slower by an amount not
  measured here; witan-code's caps assume 362ms per statement there plus a
  commit of about 4s, which puts 32 statements near 16s an attempt and five
  refused attempts with their backoff near 82s.
- The CLI's argument size. The CLI transport passes the composed source and
  its params as one argv element each, and Linux caps one at 128 KiB
  (``MAX_ARG_STRLEN``). The source costs about 1.45 KB a statement, so 128
  statements (186 KB) failed with ``Argument list too long`` where 64 (93 KB)
  passed. Params cost each row's JSON plus about 80 bytes.
- The HTTP body. A local omnigraph-server refused a mutate body above about
  1 MiB with 413.

``store_merge`` runs inside one MCP request, which APISIX cuts off at 120s
(``WITAN_REQUEST_TIMEOUT``), after it has already exported the deployed graph
and loaded the rest of the batch. So one call writes at most ONE fenced
commit, of at most ``TASKS_PER_COMMIT`` statements and ``TASK_BYTES_PER_COMMIT``
of row data, and the client splits its batches so a call never carries more
Task rows than fit in one (:func:`split_by_task_budget`). At 32 the source is
about 46 KB and the params stay under 96 KiB, both clear of the 128 KiB
argument cap, and the worst case estimated above stays under the gateway
budget with room for the export and the load. The cost is more calls for a
source heavy in tasks: every Task row counts, not only collisions, because
the client cannot tell which rows collide until the server reconciles them.
"""

from __future__ import annotations

import json

#: ``merge_task`` statements in one fenced commit, and Task rows in one
#: ``store_merge`` call.
TASKS_PER_COMMIT = 32

#: Row data in one fenced commit, counted by :func:`task_cost`. Under the CLI's
#: 128 KiB argument cap with room for the timestamp rewriting
#: ``merge_task``'s params do.
TASK_BYTES_PER_COMMIT = 96 * 1024

#: The byte budget the client splits a ``store_merge`` batch's Task rows to.
#: Lower than the server's, because the server sizes the rows after
#: ``claim_from_author`` has restamped them, and a longer author must not push
#: a batch the client sized at the edge into a second commit, which the server
#: refuses.
CLIENT_TASK_BYTES_PER_CALL = 64 * 1024

#: The first witan-council whose ``witan migrate merge`` splits batches this
#: way. Named in the server's refusal of a batch from an older client.
SPLITTING_CLIENT_VERSION = "0.43.0"


def task_cost(row: dict) -> int:
    """The bytes a Task export row counts against a byte budget here."""
    return len(json.dumps(row.get("data") or {}))


def split_by_task_budget(
    rows: list[dict], max_bytes: int | None = None
) -> list[list[dict]]:
    """``rows`` in order, split so no part holds more than ``TASKS_PER_COMMIT``
    Task rows or ``max_bytes`` of them (``TASK_BYTES_PER_COMMIT`` when
    omitted). Other rows stay in whichever part they fall in.

    A Task row over ``max_bytes`` on its own still gets a part, by itself: it
    cannot be split, and ``task_update`` would send it whole too.
    """
    budget = TASK_BYTES_PER_COMMIT if max_bytes is None else max_bytes
    parts: list[list[dict]] = []
    part: list[dict] = []
    count = size = 0
    for row in rows:
        if row.get("type") == "Task":
            cost = task_cost(row)
            if count and (count + 1 > TASKS_PER_COMMIT or size + cost > budget):
                parts.append(part)
                part, count, size = [], 0, 0
            count += 1
            size += cost
        part.append(row)
    if part:
        parts.append(part)
    return parts
