"""Reconciling what the FAILED writes actually did (concurrency_probe).

The probe's job is to detect a write whose outcome the client cannot determine.
Until 2026-08-13 it could not: verification covered only the rows the server
ACKED, so an errored write that had nonetheless committed appeared in no bucket
at all and the verdict line read `0 LOST`.

Measured against CI that day: the probe said "11 acked, 13 errored, 0 LOST"
while the pod log showed 23 of 24 handlers finishing `ok`, and twelve rows were
sitting in the graph the probe believed had failed. These pin the reconciliation
that makes that visible, and the auth classification that stops an expired token
being reported as saturation.
"""

from __future__ import annotations

import json

import pytest
from witan.scripts import concurrency_probe as cp
from witan.scripts.concurrency_probe import (
    _is_auth_failure,
    _rows_for_run,
    _writer_title,
)

LABEL = "[concurrency-probe probe-abc123]"


class _Srv:
    """A store holding ``titles``; raises if ``boom``."""

    def __init__(self, titles, boom=False):
        self._titles = titles
        self._boom = boom
        self.calls = []

    def memory_list(self, **kwargs):
        self.calls.append(kwargs)
        if self._boom:
            raise RuntimeError("store unavailable")
        return [{"title": t, "slug": f"ctx-{i}"} for i, t in enumerate(self._titles)]


def test_the_title_is_deterministic_so_an_errored_write_is_still_findable():
    """★ THE WHOLE MECHANISM. A slug carries `uuid4().hex[:6]`, so a call that
    errored — returning nothing — can never be found by slug. Its title can."""
    assert _writer_title(LABEL, 7) == f"{LABEL} writer 7"


def test_rows_for_run_matches_only_this_run():
    srv = _Srv([f"{LABEL} writer 0", "[concurrency-probe probe-other] writer 0", "x"])
    found = _rows_for_run(srv, LABEL, repo="r")
    assert list(found) == [f"{LABEL} writer 0"]


def test_rows_for_run_lists_rather_than_searches():
    """`memory_search` returns the top 20 by BM25 and a 24-writer run needs all
    of them — a cap silently reads as "the rest were never written", which is
    the exact false negative being fixed."""
    srv = _Srv([f"{LABEL} writer {i}" for i in range(24)])
    assert len(_rows_for_run(srv, LABEL, repo="r")) == 24
    assert srv.calls == [{"kind": "agent_context", "repo": "r"}]


def test_rows_for_run_returns_none_when_it_could_not_look():
    """★ `None` IS NOT `{}`. An empty listing is a successful look that found
    nothing, which licenses "absent". A failed listing establishes no absence —
    collapsing them would print "a clean refusal" about writes whose fate is
    unknown, the same false certainty the reconciliation removes."""
    assert _rows_for_run(_Srv([], boom=True), LABEL, repo="r") is None
    assert _rows_for_run(_Srv([]), LABEL, repo="r") == {}


@pytest.mark.parametrize(
    "row",
    [
        # ★ THE SHAPE A DEPLOYED 401 ACTUALLY ARRIVES IN. The outer exception
        # says only "Server returned an error response"; the status survives
        # solely in the structured field `_error_detail` recorded. Classifying
        # on prose alone counted these as capacity — the bug being fixed.
        {
            "error_type": "MCPError",
            "error": "Server returned an error response",
            "http_status": 401,
        },
        {"error_type": "MCPError", "error": "…", "http_status": 403},
        # A client that classified it itself may carry no status at all.
        {"error_type": "RemoteCredentialRejected", "error": "…"},
        {"error_type": "X", "error": "it rejected the credential"},
        {"error_type": "MCPError", "error": "401 Unauthorized"},
    ],
)
def test_an_auth_failure_is_recognised(row):
    assert _is_auth_failure(row)


@pytest.mark.parametrize(
    "row",
    [
        {"error_type": "TimeoutExpired", "error": "phase deadline reached"},
        {"error_type": "MCPError", "error": "Server returned an error response"},
        # A non-auth status must not be swept in by the structured check.
        {"error_type": "MCPError", "error": "…", "http_status": 502},
        # `-32603` contains "401"-adjacent digits in some payloads; the prose
        # arm must not match on a bare number for that reason.
        {"error_type": "MCPError", "error": "error_code=-32603 code 4013"},
        {},
    ],
)
def test_a_capacity_failure_is_not_mistaken_for_auth(row):
    """The direction that matters: calling saturation "auth" would excuse the
    very failure the probe exists to measure."""
    assert not _is_auth_failure(row)


class _Boom:
    """A proxy whose warmup succeeds and whose measured call fails."""

    def task_get(self, **_):
        return {"slug": "tk-warmup"}

    def memory_store(self, **_):
        raise RuntimeError("Server returned an error response")


def test_an_errored_worker_still_reports_when_it_fired(monkeypatch, capsys):
    """★ THE SAME BLIND SPOT AS THE SLUGS, in the timing bookkeeping.

    `fired_at` used to be assigned after the call returned, so only successes
    carried one. At the loads worth measuring most workers error, and a
    perfectly simultaneous 24-way burst then reported `only 1 worker(s) fired at
    all` — while probe C's write window, built from the same stamps, collapsed
    and declared `no read overlapped any write` over readers that were in the
    middle of the storm. Both were observed on CI on 2026-08-13.
    """
    monkeypatch.setattr(cp, "_srv", lambda *a, **k: _Boom())
    cp._worker(
        "store",
        index=3,
        start_at=0.0,
        payload={
            "target": "ci",
            "warmup_slug": "tk-warmup",
            "label": LABEL,
            "run_id": "probe-abc123",
            "repo": "https://github.com/mitodl/agent-kit",
        },
    )
    row = json.loads(capsys.readouterr().out.strip())

    assert row["ok"] is False
    assert row["fired_at"] > 0
    # done_at too: a failed call still occupied the server for as long as it
    # took to fail, and that interval is the one probe C overlaps against.
    assert row["done_at"] >= row["fired_at"]
    assert cp._timing([row], start_at=0.0).n_fired == 1


def test_a_worker_that_never_reached_the_barrier_claims_no_window(monkeypatch, capsys):
    """The other direction: a worker that died in warmup did NOT fire, and
    stamping it would manufacture contention out of a crash."""

    class _DeadOnWarmup:
        def task_get(self, **_):
            raise RuntimeError("connect refused")

    monkeypatch.setattr(cp, "_srv", lambda *a, **k: _DeadOnWarmup())
    cp._worker(
        "store",
        index=0,
        start_at=0.0,
        payload={"target": "ci", "warmup_slug": "tk-warmup"},
    )
    row = json.loads(capsys.readouterr().out.strip())

    assert row["ok"] is False
    assert "fired_at" not in row
    assert "done_at" not in row
    assert cp._timing([row], start_at=0.0).n_fired == 0


def _row(index, fired, done, **kw):
    return {"index": index, "fired_at": fired, "done_at": done, **kw}


def test_the_write_window_spans_the_errored_writers_too():
    """★ At the loads worth measuring most writers ERROR. A window drawn from
    the survivors alone is too narrow, and in an all-errored phase there is no
    window at all — which is how probe C came to report "no read overlapped any
    write" over readers that were in the middle of the storm."""
    rows = [
        _row(0, 100.0, 130.0, ok=False),
        _row(1, 100.1, 105.0, ok=True),
        _row(2, 100.2, 145.0, ok=False),
    ]
    assert cp._write_window(rows) == (100.0, 145.0)


def test_a_writer_that_never_fired_contributes_no_window():
    assert cp._write_window([{"index": 0, "ok": False}]) is None


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (_row(0, 110.0, 120.0), True),  # wholly inside
        (_row(1, 90.0, 105.0), True),  # overlaps the start
        (_row(2, 140.0, 160.0), True),  # overlaps the end
        (_row(3, 80.0, 99.0), False),  # finished before the storm
        (_row(4, 151.0, 160.0), False),  # started after it
        ({"index": 5, "ok": False}, False),  # never fired
    ],
)
def test_overlap_is_interval_intersection_not_containment(row, expected):
    """A reader that started before the first write and was still running when
    it landed WAS under load; requiring containment would discard exactly the
    slowest reads, which are the ones the storm produced."""
    assert cp._in_window(row, (100.0, 150.0)) is expected


def test_nothing_overlaps_an_absent_window():
    assert cp._in_window(_row(0, 100.0, 110.0), None) is False
