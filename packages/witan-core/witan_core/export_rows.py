"""Turn ``omnigraph export`` rows into rows a format-9 graph will load.

An export taken by an older binary does not load into omnigraph 0.11 as it
stands, and a merge routinely feeds one in: ``witan migrate storage`` rebuilds
a local store from the old binary's export, and ``witan migrate merge`` accepts
an export taken on another machine long before. Three things differ, each
measured against the 0.10.0 and 0.11.0 binaries (see
docs/internals/design/omnigraph-0-11-upgrade-spec.md, D2):

1. A node's identity moved from ``data.id`` to a top-level ``id``. 0.11 refuses
   the old spelling outright.
2. Every witan edge type is keyed on ``(@src, @dst)``, so the engine derives an
   edge's id from its endpoints. A 0.10 edge row carries a ULID in ``data.id``,
   and 0.11 refuses that id on a keyed edge ("does not match its canonical @key
   id"), so it is dropped rather than moved.
3. 0.10 graphs hold duplicate edge rows for one pair, because unkeyed edges
   appended on every re-link. Two rows with one key in a single load fail the
   WHOLE load (``@unique violation``), so duplicates are collapsed to one.

Collapsing works over the whole row set, never one record at a time: duplicates
of one pair can sit anywhere in an export, and a caller that chunks rows into
batches has to collapse first or a pair split across two batches survives.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

#: omnigraph 0.9 and 0.10 export a ``DateTime`` as integer milliseconds since
#: the Unix epoch, UTC. NOT microseconds — ``commit list --json`` uses
#: microseconds for its own ``created_at``, and the two surfaces genuinely
#: disagree. Getting this scale wrong does not raise; it silently dates every
#: row to January 1970 and quietly inverts merge decisions. Measured on 0.9.0:
#: ``"2026-01-01T00:00:00Z"`` exports as ``1767225600000``.
EXPORT_TS_PER_SECOND = 1_000

#: How a link came to exist (witan schema.pg § Memory edge properties). A named
#: link outranks a derived one, which is the invariant witan's re-tag check
#: protects when an auto-derived Tagged edge meets an asserted one.
_CONFIDENCE_RANK = {"asserted": 2, "inferred": 1}


def parse_export_ts(value: str | int | float | None) -> datetime | None:
    """Parse an exported timestamp for comparison, or ``None`` if absent/unusable.

    THREE REPRESENTATIONS, because a merge routinely spans omnigraph versions.
    0.8.x exports naive ISO-8601 strings; 0.9.x and 0.10.x export integer epoch
    milliseconds (``EXPORT_TS_PER_SECOND``); 0.11 is back to naive ISO strings,
    without a trailing ``Z`` and without a fractional part when it is zero. An
    export file outlives the store that produced it, so every form has to keep
    working.

    Everything normalizes to naive UTC so any two values are comparable —
    ``datetime`` raises on comparing an aware value against a naive one. The
    string form needs this because two stores' text isn't guaranteed to share a
    format (``Z`` vs. ``+00:00``, or genuinely different offsets), and
    comparing raw strings sorts wrong across those: ``"...T23:30:00-05:00"``,
    later in UTC, sorts *before* ``"...T00:00:00Z"`` the next calendar day.

    An unusable value degrades to ``None`` rather than raising, since a
    malformed value shouldn't crash a merge — it just can't win a comparison.
    ``bool`` is excluded deliberately: it is an ``int`` subclass, and ``True``
    would otherwise read as 1ms past the epoch.
    """
    if not value:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(
                value / EXPORT_TS_PER_SECOND, timezone.utc
            ).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def is_edge(row: object) -> bool:
    """Whether an export record is an edge.

    A node is ``{"type", "data"}`` and an edge is ``{"edge", "from", "to",
    "data"}``: an edge carries no ``type`` at all, and that asymmetry is the
    only discriminator an export offers.
    """
    return isinstance(row, dict) and "type" not in row and bool(row.get("edge"))


def edge_key(row: dict) -> tuple[str, str, str]:
    """The identity a keyed edge has in a format-9 graph: its type and endpoints."""
    return (row["edge"], row.get("from"), row.get("to"))


def edge_rank(row: dict) -> tuple[int, datetime]:
    """How strongly an edge row claims its pair; the higher rank is kept.

    Confidence first (asserted > inferred > unset), then ``created_at`` with an
    unset value oldest. Confidence outranks recency because an asserted link is
    what a caller named, and ``created_at`` is null on every witan edge written
    before 2026-09, so recency alone would pick arbitrarily among most of them.
    """
    data = row.get("data") or {}
    return (
        _CONFIDENCE_RANK.get(data.get("confidence"), 0),
        parse_export_ts(data.get("created_at")) or datetime.min,
    )


def _node_with_top_level_id(row: object) -> object:
    if not isinstance(row, dict) or "type" not in row or "id" in row:
        return row
    data = row.get("data")
    if not isinstance(data, dict) or "id" not in data:
        return row
    data = dict(data)
    moved = {**row, "id": data.pop("id")}
    moved["data"] = data
    return moved


def _edge_without_id(row: dict) -> dict:
    stripped = {k: v for k, v in row.items() if k != "id"}
    data = stripped.get("data")
    if isinstance(data, dict) and "id" in data:
        stripped["data"] = {k: v for k, v in data.items() if k != "id"}
    return stripped


def normalize_export(rows: Iterable[object]) -> tuple[list, int]:
    """Rows ready to load into a keyed format-9 graph, and how many edges collapsed.

    Nodes get a top-level ``id`` (moved from ``data.id``; a row already carrying
    one is left alone, so a 0.11 export passes through unchanged). Edges lose
    ``id`` entirely and are collapsed to one row per ``(edge, from, to)``: the
    highest :func:`edge_rank` wins, and on a tie the later row wins. The winner
    is kept WHOLE — its properties are never combined with a loser's, which
    would store an edge nobody wrote.

    Records that are neither a node nor an edge (including non-objects) pass
    through untouched, so the caller's own validation still reports them.
    Input rows are not mutated.

    The second value is the number of edge rows dropped by the collapse, so a
    caller that accounts for every source row can say where they went.
    """
    out: list = []
    slot: dict[tuple[str, str, str], int] = {}
    collapsed = 0
    for row in rows:
        if not is_edge(row):
            out.append(_node_with_top_level_id(row))
            continue
        edge = _edge_without_id(row)
        key = edge_key(edge)
        at = slot.get(key)
        if at is None:
            slot[key] = len(out)
            out.append(edge)
            continue
        collapsed += 1
        if edge_rank(edge) >= edge_rank(out[at]):
            out[at] = edge
    return out, collapsed
