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

import pytest

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


def test_rows_for_run_never_raises():
    """It runs after the measurement. A failure to reconcile must degrade the
    verdict to "found nothing", not destroy a phase's results."""
    assert _rows_for_run(_Srv([], boom=True), LABEL, repo="r") == {}


@pytest.mark.parametrize(
    "row",
    [
        {"error_type": "RemoteCredentialRejected", "error": "…"},
        {"error_type": "MCPError", "error": "server returned 401 Unauthorized"},
        {"error_type": "X", "error": "it rejected the credential"},
    ],
)
def test_an_auth_failure_is_recognised(row):
    """Matched on the message because a worker is a separate PROCESS and hands
    back JSON, not an exception — and both the classified client error and the
    raw status have to be accepted."""
    assert _is_auth_failure(row)


@pytest.mark.parametrize(
    "row",
    [
        {"error_type": "TimeoutExpired", "error": "exceeded 90s"},
        {"error_type": "MCPError", "error": "Server returned an error response"},
        {},
    ],
)
def test_a_capacity_failure_is_not_mistaken_for_auth(row):
    """The direction that matters: calling saturation "auth" would excuse the
    very failure the probe exists to measure."""
    assert not _is_auth_failure(row)
