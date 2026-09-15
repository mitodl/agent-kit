"""Normalizing ``omnigraph export`` rows for a keyed format-9 load."""

import copy
from datetime import datetime

from witan_core.export_rows import (
    edge_rank,
    normalize_export,
    parse_export_ts,
)

# 2026-01-01T00:00:00Z and 2026-01-02T00:00:00Z as 0.10 exports them.
_JAN_1_MS = 1767225600000
_JAN_2_MS = 1767312000000


def _tagged(
    confidence=None, created_at=None, role=None, ulid="01M2KBRXAH26XMG18YDFJZ7WF4"
):
    """A Tagged edge row in 0.10's export shape: identity in `data.id`."""
    return {
        "edge": "Tagged",
        "from": "pat-a",
        "to": "tp-topic-uv",
        "data": {
            "id": ulid,
            "author": "t",
            "confidence": confidence,
            "created_at": created_at,
            "role": role,
        },
    }


def test_a_010_node_has_its_id_moved_to_the_top_level():
    row = {"type": "Memory", "data": {"id": "pat-a", "slug": "pat-a", "title": "A"}}

    (out,), collapsed = normalize_export([row])

    assert out == {
        "type": "Memory",
        "id": "pat-a",
        "data": {"slug": "pat-a", "title": "A"},
    }
    assert collapsed == 0


def test_a_011_node_passes_through_unchanged():
    row = {"type": "Memory", "id": "pat-a", "data": {"slug": "pat-a"}}

    (out,), _ = normalize_export([row])

    assert out == row


def test_a_node_already_carrying_a_top_level_id_keeps_both_as_they_are():
    """Upstream's rule is `if has("id") then . else ...`: a row that already
    states its identity is not second-guessed."""
    row = {"type": "Memory", "id": "pat-a", "data": {"id": "other", "slug": "pat-a"}}

    (out,), _ = normalize_export([row])

    assert out == row


def test_an_edge_loses_its_id_in_both_spellings():
    old = _tagged()
    new = {
        "edge": "Blocks",
        "id": '["tk-two","tk-one"]',
        "from": "tk-two",
        "to": "tk-one",
        "data": {},
    }

    out, _ = normalize_export([old, new])

    assert "id" not in out[0] and "id" not in out[0]["data"]
    assert out[1] == {"edge": "Blocks", "from": "tk-two", "to": "tk-one", "data": {}}


def test_an_older_asserted_edge_beats_a_newer_inferred_one():
    asserted = _tagged(confidence="asserted", created_at=_JAN_1_MS, role="named")
    inferred = _tagged(confidence="inferred", created_at=_JAN_2_MS)

    out, collapsed = normalize_export([asserted, inferred])

    assert collapsed == 1
    assert out == [
        {
            "edge": "Tagged",
            "from": "pat-a",
            "to": "tp-topic-uv",
            "data": {
                "author": "t",
                "confidence": "asserted",
                "created_at": _JAN_1_MS,
                "role": "named",
            },
        }
    ]


def test_at_equal_confidence_the_newer_edge_wins_and_an_unset_timestamp_is_oldest():
    stamped = _tagged(confidence="asserted", created_at=_JAN_1_MS, role="stamped")
    unset = _tagged(confidence="asserted", created_at=None, role="unset")

    out, _ = normalize_export([stamped, unset])

    assert [r["data"]["role"] for r in out] == ["stamped"]


def test_timestamps_compare_across_010_millis_and_011_strings():
    """A merge can pair a 0.10 export with a 0.11 one, so the two spellings of
    a DateTime have to order against each other."""
    old = _tagged(confidence="asserted", created_at=_JAN_2_MS, role="jan-2-millis")
    new = _tagged(
        confidence="asserted", created_at="2026-01-01T12:00:00", role="jan-1-string"
    )

    out, _ = normalize_export([old, new])

    assert [r["data"]["role"] for r in out] == ["jan-2-millis"]


def test_on_a_tie_the_later_row_wins():
    first = _tagged(role="first")
    second = _tagged(role="second")

    out, _ = normalize_export([first, second])

    assert [r["data"]["role"] for r in out] == ["second"]


def test_the_winner_is_kept_whole_never_combined_with_a_loser():
    """A loser's non-null property must not fill a winner's null one: that
    would store an edge nobody wrote."""
    winner = _tagged(confidence="asserted", created_at=_JAN_2_MS, role=None)
    loser = _tagged(confidence="inferred", created_at=_JAN_1_MS, role="from-the-loser")

    out, _ = normalize_export([loser, winner])

    assert out[0]["data"]["role"] is None


def test_duplicates_far_apart_in_the_export_still_collapse():
    """The reason collapse runs over the whole row set: split the same rows
    into load batches first and a pair divided between two of them survives,
    failing the second batch's keyed load."""
    rows = [
        _tagged(confidence="inferred", created_at=_JAN_1_MS),
        *(
            {"type": "Memory", "data": {"id": f"m-{n}", "slug": f"m-{n}"}}
            for n in range(50)
        ),
        _tagged(confidence="asserted", created_at=_JAN_1_MS, role="late"),
        {"edge": "Blocks", "from": "tk-two", "to": "tk-one", "data": {"id": "01X"}},
        _tagged(confidence="inferred", created_at=_JAN_2_MS),
    ]

    out, collapsed = normalize_export(rows)

    edges = [r for r in out if "edge" in r]
    assert collapsed == 2
    assert len(out) == len(rows) - collapsed
    assert [(e["edge"], e["data"].get("role")) for e in edges] == [
        ("Tagged", "late"),
        ("Blocks", None),
    ]


def test_distinct_pairs_and_distinct_edge_types_are_not_collapsed():
    rows = [
        _tagged(),
        {**_tagged(), "to": "tp-topic-x"},
        {**_tagged(), "edge": "RelatedTo"},
    ]

    out, collapsed = normalize_export(rows)

    assert collapsed == 0
    assert len(out) == 3


def test_the_input_rows_are_not_mutated():
    rows = [
        {"type": "Memory", "data": {"id": "pat-a", "slug": "pat-a"}},
        _tagged(confidence="asserted"),
        _tagged(confidence="inferred"),
    ]
    before = copy.deepcopy(rows)

    normalize_export(rows)

    assert rows == before


def test_records_that_are_neither_node_nor_edge_pass_through_for_the_caller_to_reject():
    rows = [[], None, "a string", {"data": {"slug": "x"}}]

    out, collapsed = normalize_export(rows)

    assert out == rows
    assert collapsed == 0


def test_edge_rank_orders_confidence_before_recency():
    assert edge_rank(_tagged(confidence="asserted", created_at=None)) > edge_rank(
        _tagged(confidence="inferred", created_at=_JAN_2_MS)
    )
    assert edge_rank(_tagged(created_at=_JAN_1_MS)) > edge_rank(
        _tagged(created_at=None)
    )


def test_parse_export_ts_reads_every_export_spelling_as_the_same_instant():
    assert parse_export_ts(_JAN_1_MS) == datetime(2026, 1, 1)
    assert parse_export_ts("2026-01-01T00:00:00") == datetime(2026, 1, 1)
    assert parse_export_ts("2026-01-01T00:00:00Z") == datetime(2026, 1, 1)
    assert parse_export_ts("2026-08-10T12:30:45.123") == datetime(
        2026, 8, 10, 12, 30, 45, 123000
    )


def test_parse_export_ts_degrades_unusable_values_to_none():
    for value in (None, "", "not a timestamp", True, False, 10**30):
        assert parse_export_ts(value) is None
