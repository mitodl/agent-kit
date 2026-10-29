"""The pooled HTTP transport, and the routing that decides when it is used.

Two things are being protected here and they fail in opposite directions.

The first is the ROUTING. Sending a call over HTTP that should have gone to the
CLI is not a slow path, it is a wrong one — a branched witan-code client whose
``--branch`` is dropped executes against ``main``, writing a WIP reindex into the
shared graph. Those tests are about what must NOT take this path.

The second is the ERROR TRANSLATION. The subprocess path's retry policy is tuned
against conditions it recognizes from CLI stderr text: a restarting server, the
per-actor admission cap, optimistic-concurrency drift, a store needing repair.
Every one of those has to keep working when the same condition arrives as an HTTP
status instead, because the policy loop is now shared and a mistranslation makes
it silently do nothing. A 429 read as a fatal error stops retrying; a mid-flight
write failure read as a connect failure re-sends a write that may have committed.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import shutil
import socket
import threading
from typing import ClassVar

import pytest

from witan_core import omnigraph as og
from witan_core import omnigraph_http as ogh
from witan_core.omnigraph import OmnigraphClient, OmnigraphConflict

QUERY_SOURCE = """
query find_memory($slug: String) {
    match (m: Memory) where m.slug == $slug
    return m.slug, m.title
}
"""


@pytest.fixture(autouse=True)
def _isolate_transport_cache(monkeypatch):
    """Transports are cached process-wide by server url (deliberately — see
    ``_TRANSPORTS``), so a test must not inherit a previous test's pool."""
    monkeypatch.setattr(og, "_TRANSPORTS", {})
    monkeypatch.setattr(og, "_QUERY_TEXT_CACHE", {})


# ── a scriptable stand-in for http.client ────────────────────────────


class FakeResponse:
    # What http.client reports for a chunked body: no Content-Length to owe.
    length = None

    def __init__(
        self,
        status: int,
        body: str = "",
        will_close: bool = False,
        headers: dict | None = None,
        stamped: bool = True,
        fail_after_body: BaseException | None = None,
    ):
        """``stamped`` adds the contract header an omnigraph-server 0.13
        puts on every response. Pass ``False`` for a 0.11 server or a proxy
        answering in the server's place.

        ``fail_after_body`` is raised by a sized ``read()`` once ``body`` has
        been handed out, the way an aborted chunked body fails part-way."""
        self.status = status
        self.will_close = will_close
        self._body = body.encode()
        self._offset = 0
        self._fail_after_body = fail_after_body
        # A real HTTPMessage, so repeated headers and case-insensitive lookup
        # behave as they do on a live response. A list value repeats the header.
        self.headers = http.client.HTTPMessage()
        if stamped:
            self.headers.add_header(ogh.HTTP_API_CONTRACT_HEADER, ogh.HTTP_API_CONTRACT)
        for name, value in (headers or {}).items():
            for item in value if isinstance(value, list) else [value]:
                self.headers.add_header(name, item)

    def read(self, amt: int | None = None) -> bytes:
        if amt is None:
            return self._body
        chunk = self._body[self._offset : self._offset + amt]
        self._offset += len(chunk)
        if not chunk and self._fail_after_body is not None:
            raise self._fail_after_body
        return chunk

    def getheader(self, name: str, default=None):
        return self.headers.get(name, default)


class FakeConnection:
    """One scripted connection. ``script`` is consumed one entry per request.

    An entry is either a :class:`FakeResponse` or an exception instance, which
    is raised from ``request()`` — i.e. after the connection was established,
    which is the mid-flight case the policy must treat differently from a
    connect failure.
    """

    created: ClassVar[list[FakeConnection]] = []

    connect_error: BaseException | None = None
    script: ClassVar[list] = []

    def __init__(self, host, port=None, timeout=None, context=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.requests: list[dict] = []
        self.closed = False
        self.thread = threading.current_thread().name
        FakeConnection.created.append(self)

    def connect(self):
        if FakeConnection.connect_error is not None:
            raise FakeConnection.connect_error

    def request(self, method, path, body=None, headers=None):
        self.requests.append(
            {"method": method, "path": path, "body": body, "headers": headers or {}}
        )
        nxt = FakeConnection.script[0]
        if isinstance(nxt, BaseException):
            FakeConnection.script.pop(0)
            raise nxt

    def getresponse(self):
        return FakeConnection.script.pop(0)

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _fake_http(monkeypatch):
    FakeConnection.created = []
    FakeConnection.script = []
    FakeConnection.connect_error = None
    monkeypatch.setattr(ogh.http.client, "HTTPConnection", FakeConnection)
    monkeypatch.setattr(ogh.http.client, "HTTPSConnection", FakeConnection)
    return FakeConnection


def ok(payload: dict, **kwargs) -> FakeResponse:
    return FakeResponse(200, json.dumps(payload), **kwargs)


def err(status: int, message: str, code: str | None = None) -> FakeResponse:
    body = {"error": message}
    if code:
        body["code"] = code
    return FakeResponse(status, json.dumps(body))


# ── classification: HTTP conditions onto the shared vocabulary ───────

_GRAPH_BLOCKED = (
    "graph is unavailable; an operator must apply an explicit correction or "
    "restart after fixing startup configuration (HTTP 503, graph_unavailable)"
)
_GRAPH_LOADING = (
    "graph is loading; wait for its startup attempt to complete "
    "(HTTP 503, graph_unavailable)"
)
_GRAPH_TRANSITIONING = (
    "graph admission is closed for a serving transition (HTTP 503, graph_unavailable)"
)


@pytest.mark.parametrize(
    ("status", "message", "expected"),
    [
        # The cap is the one the CLI could only guess at from message text; over
        # HTTP it is unambiguous, and it must keep its own backoff budget.
        (429, "in-flight count cap exceeded", ogh.ADMISSION_CAP),
        (429, "anything at all", ogh.ADMISSION_CAP),
        # Answered-but-not-serving. The response proves the request was rejected
        # rather than applied, so it is safe to retry even for a write.
        (503, "service unavailable", ogh.UNAVAILABLE),
        (409, "stale view; refresh and retry", ogh.RETRYABLE),
        # ★ THE SAME STATUS WITH A MESSAGE NO MARKER MATCHES. Verbatim from a QA
        # `task_claim` race, 2026-08-17. The line above passed on its PROSE, so
        # it never established that a 409 is retryable — and this one fell
        # through to FATAL, which is what broke the compare-and-swap path. The
        # status is now the signal, so the server may reword the precondition
        # (it has, twice) without silently making a losable race fatal again.
        (
            409,
            (
                "write authority 'graph_head:main' changed during preparation "
                "(expected 01M08E24Y2WWC3QVE9MD3K6CWN, current 01M08E27K5J56HF8QZ7GX61X3F)"
                " — reprepare from the current branch state"
            ),
            ogh.RETRYABLE,
        ),
        # Prose the classifier has never seen, on a status that speaks for
        # itself. This is the case the status rule exists for.
        (409, "some future conflict wording nobody has written yet", ogh.RETRYABLE),
        (500, "manifest table version mismatch", ogh.RETRYABLE),
        (500, "head is ahead of manifest, run omnigraph repair", ogh.NEEDS_REPAIR),
        # ★ 412 IS THE INVERSE OF THE 409 ABOVE, and the pair is parametrised
        # together on purpose: they look like the same family and mean opposite
        # things. 409 = the head moved, re-send. 412 = the condition you stated
        # is false, do NOT re-send.
        (412, "precondition_failure", ogh.PRECONDITION_FAILED),
        (
            412,
            "graph commit 01M08E24Y… no longer current",
            ogh.PRECONDITION_FAILED,
        ),
        # A 4xx/5xx carrying the wording but not the status — the server is
        # entitled to reword, and the body is a second, independent signal.
        # (NOT a 200: `_send` returns OK for any 2xx without ever consulting
        # this function, so a 200 case here would test an unreachable path and
        # say nothing about the CLI classifier, which lives in omnigraph.py and
        # is covered in test_omnigraph.py.)
        (500, "precondition_failure {expected: A, actual: B}", ogh.PRECONDITION_FAILED),
        # ★ NOT EVERY 503 IS "WAIT FOR THE SERVER". This one warns that table
        # effects may already need recovery, so the blanket UNAVAILABLE retry
        # would re-apply a partly-present write.
        (
            503,
            (
                "recovery required for operation 01KZY…: pending Load recovery "
                "operation blocks writes on branch 'main'"
            ),
            ogh.RECOVERY_REQUIRED,
        ),
        (503, "recovery_required", ogh.RECOVERY_REQUIRED),
        # …but a plain 503 still is.
        (503, "service temporarily overloaded", ogh.UNAVAILABLE),
        # The three 503 graph_unavailable bodies of a 0.13 server. Same status
        # and code; only the blocked one does not clear by waiting. The first
        # is verbatim from 0.13.0, the other two from upstream's handlers.rs.
        (503, _GRAPH_BLOCKED, ogh.FATAL),
        (503, _GRAPH_LOADING, ogh.UNAVAILABLE),
        (503, _GRAPH_TRANSITIONING, ogh.UNAVAILABLE),
        # A denial is not a transient condition. Retrying a Cedar denial just
        # burns the budget and reports the same thing 8 attempts later.
        (403, "policy denied action 'change' for unknown actor 'act-x'", ogh.FATAL),
        (401, "invalid bearer token", ogh.FATAL),
        (404, "graph not found", ogh.FATAL),
    ],
)
def test_status_classification(status, message, expected):
    assert ogh.classify_status(status, message) == expected


def test_repair_wins_over_retryable_when_both_words_appear():
    """A message naming both conditions has to resolve to drift, or the client
    retries a store that cannot succeed until it is reconciled.
    """
    message = "stale view: head is ahead of manifest, run omnigraph repair"
    assert ogh.classify_status(500, message) == ogh.NEEDS_REPAIR


def test_repair_still_wins_over_a_conflict_status():
    """The message markers must stay ahead of the 409 rule, not behind it.

    A store that needs reconciling cannot be fixed by trying again, however the
    status is labelled — so a 409 that names drift must still be reported as
    drift. Ordering is the only thing enforcing that, hence a test on it.
    """
    message = "head is ahead of manifest, run omnigraph repair"
    assert ogh.classify_status(409, message) == ogh.NEEDS_REPAIR


def test_an_in_doubt_publish_is_not_retried():
    """From 0.12 the publisher reads a commit back when its acknowledgement is
    lost and answers success if it landed. Only a read-back that cannot
    complete reaches the client: a 500 ``internal`` whose message names the
    commit (upstream ``omnigraph-catalog`` ``publish_outcome_in_doubt``,
    v0.13.0). The write may be durable, so nothing may re-send it."""
    body = json.dumps(
        {
            "error": (
                "manifest publish outcome is in doubt: graph commit "
                "hb1.01M52GVAKWPB0MSGP9MTPJMR1Z.2.01M52GVAMZ8HEBWP8PGNGD0JZS may "
                "be durable but it could not be confirmed (connection reset); "
                "reopen the graph and look that commit up before retrying a "
                "non-idempotent write. original error: storage: connection reset"
            ),
            "code": "internal",
        }
    )
    assert ogh.classify_status(500, ogh.error_message(500, body)) == ogh.FATAL


def test_full_text_rebuild_required_is_terminal_not_a_retryable_conflict():
    """omnigraph 0.10.0 (upstream #581) answers a stale full-text index with a
    409 carrying `full_text_index_rebuild_required`. The bare-409 rule below it
    would retry that to exhaustion and bury the remedy, so this must be caught
    on the structured key AND on the server's prose."""
    body = json.dumps(
        {
            "error": (
                "full-text index 'memory_content' requires rebuild: analyzer "
                "generation cannot be proven compatible; run omnigraph "
                "rebuild-full-text-indexes <URI> --branch <branch> on the live "
                "branch (historical snapshots are unchanged)"
            ),
            "code": "conflict",
            "details": {
                "full_text_index_rebuild_required": {
                    "index": "memory_content",
                    "reason": "analyzer generation cannot be proven compatible",
                }
            },
        }
    )
    assert ogh.classify_status(409, body) == ogh.FULL_TEXT_REBUILD_REQUIRED
    assert ogh.classify_status(409, body) != ogh.RETRYABLE

    # The detail key alone is enough — a relay that drops the prose still
    # classifies correctly.
    assert (
        ogh.classify_status(
            409, '{"details": {"full_text_index_rebuild_required": {}}}'
        )
        == ogh.FULL_TEXT_REBUILD_REQUIRED
    )


def test_a_plain_conflict_is_still_retryable():
    """The guard above must not swallow the ordinary lost-race 409 that
    `task_claim` depends on."""
    message = (
        "write authority 'graph_head:main' changed during preparation "
        "(expected 01M08E24Y, current 01M08E27K) - reprepare from the "
        "current branch state"
    )
    assert ogh.classify_status(409, message) == ogh.RETRYABLE


def test_error_message_uses_the_servers_own_wording():
    body = json.dumps({"error": "policy denied action 'change'", "code": "forbidden"})
    assert ogh.error_message(403, body) == (
        "policy denied action 'change' (HTTP 403, forbidden)"
    )


def test_error_message_passes_through_a_non_json_body():
    """An ingress failing in FRONT of the server answers with HTML, not our
    error envelope. Swallowing it would make the most confusing deployment
    failure unreadable."""
    message = ogh.error_message(502, "<html>502 Bad Gateway</html>")
    assert "502" in message
    assert "Bad Gateway" in message


def test_error_message_survives_an_empty_body():
    assert ogh.error_message(500, "") == "HTTP 500"


# ── the server's Retry-After, which only this transport can see ──────
# The CLI's error path prints the JSON body's message and discards the response
# headers, which is why the admission-cap backoff was a blind schedule. Over
# HTTP the response object is right here.


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("60", 60.0),
        ("0", 0.0),
        ("  30 ", 30.0),
        (None, None),
        # An HTTP-date is legal per RFC 9110 and deliberately unread: honouring
        # it means trusting this clock against the server's, and a skewed clock
        # turns "wait a minute" into "wait an hour" or into no wait at all.
        ("Wed, 12 Aug 2026 14:00:00 GMT", None),
        ("", None),
        ("soon", None),
        # A negative delay is not what the server asked for, and reading it as
        # zero would silently turn a malformed header into a hot retry loop.
        ("-5", None),
    ],
)
def test_parse_retry_after(header, expected):
    assert ogh.parse_retry_after(header) == expected


def test_a_429_carries_the_servers_retry_after(_fake_http):
    _fake_http.script = [
        FakeResponse(
            429,
            json.dumps({"error": "actor in-flight count cap exceeded"}),
            headers={"Retry-After": "60"},
        )
    ]
    transport = ogh.PooledTransport("http://host:8080")

    outcome = transport.mutate("council", "query q() {}", {}, "tok")

    assert outcome.kind == ogh.ADMISSION_CAP
    assert outcome.retry_after == 60.0


def test_a_response_without_the_header_reports_no_hint(_fake_http):
    """None means "the server said nothing", NOT "retry immediately" — the
    policy loop falls back to its own schedule on it."""
    _fake_http.script = [FakeResponse(429, json.dumps({"error": "cap exceeded"}))]
    transport = ogh.PooledTransport("http://host:8080")

    assert transport.mutate("council", "query q() {}", {}, "tok").retry_after is None


# ── connect vs mid-flight: the write-safety distinction ──────────────


def test_connect_failure_is_unavailable(_fake_http):
    """Provably pre-send, so the policy may retry it even for a mutate."""
    _fake_http.connect_error = ConnectionRefusedError("connection refused")
    transport = ogh.PooledTransport("http://host:8080")

    outcome = transport.mutate("council", "query q() {}", {}, "tok")

    assert outcome.kind == ogh.UNAVAILABLE
    assert "could not connect" in outcome.error


def test_dns_failure_is_unavailable(_fake_http):
    _fake_http.connect_error = socket.gaierror("name resolution failed")
    transport = ogh.PooledTransport("http://host:8080")

    assert transport.query("council", "q", {}, None).kind == ogh.UNAVAILABLE


def test_midflight_failure_on_a_write_is_fatal_even_on_a_fresh_connection(_fake_http):
    """Opening the connection yourself does NOT make a mid-flight failure safe.

    `connect()` returning means the TCP handshake completed, not that the
    request was never written — and this same `except` also covers
    `getresponse()` and `read()`, by which point it definitely was and the
    server may already have committed it. Classifying this as UNAVAILABLE hands
    the write to a retry loop that re-sends it, silently, one layer below where
    `surface_conflict` would let a caller object.
    """
    _fake_http.script = [ConnectionResetError("reset")]
    transport = ogh.PooledTransport("http://host:8080")

    assert transport.mutate("council", "q", {}, None).kind == ogh.FATAL


def test_midflight_failure_on_a_read_is_unavailable(_fake_http):
    """The complement: re-running a query is harmless, so a read may ride the
    restart budget rather than failing a whole tool call over a blip."""
    _fake_http.script = [ConnectionResetError("reset")]
    transport = ogh.PooledTransport("http://host:8080")

    assert transport.query("council", "q", {}, None).kind == ogh.UNAVAILABLE


def test_a_write_is_never_retried_after_the_response_began(_fake_http):
    """The most dangerous shape: the server ANSWERED and we failed reading the
    body. The mutation is committed; a retry would apply it twice."""
    _fake_http.script = [ConnectionResetError("reset while reading body")]
    transport = ogh.PooledTransport("http://host:8080")

    outcome = transport.mutate("council", "q", {}, None)

    assert outcome.kind == ogh.FATAL
    assert len(_fake_http.created) == 1, "must not open a second connection"


def test_connect_failure_stays_retryable_for_a_write(_fake_http):
    """The one case that IS provably pre-send, and must not be lost to the fix
    above — otherwise a routine server restart fails every write outright."""
    _fake_http.connect_error = ConnectionRefusedError("connection refused")
    transport = ogh.PooledTransport("http://host:8080")

    assert transport.mutate("council", "q", {}, None).kind == ogh.UNAVAILABLE


def test_stale_keepalive_is_retried_for_a_read(_fake_http):
    """The server closed an idle connection; we find out by using it.

    Safe to repeat for a query, and doing so is what keeps a pooled connection
    from turning routine idleness into a user-visible error.
    """
    transport = ogh.PooledTransport("http://host:8080")
    _fake_http.script = [ok({"rows": []})]
    transport.query("council", "q", {}, None)  # establishes the pooled connection
    assert len(_fake_http.created) == 1

    _fake_http.script = [
        http.client.RemoteDisconnected("closed"),
        ok({"rows": [{"m.slug": "a"}]}),
    ]
    outcome = transport.query("council", "q", {}, None)

    assert outcome.kind == ogh.OK
    assert json.loads(outcome.body)["rows"] == [{"m.slug": "a"}]
    assert len(_fake_http.created) == 2, "the retry must use a fresh connection"


def test_stale_keepalive_is_not_retried_for_a_write(_fake_http):
    """The request may have been sent and committed. Re-applying it is exactly
    what `surface_conflict` exists to prevent, so it must not happen here
    either — silently, one layer lower, where no caller can opt out."""
    _fake_http.script = [ok({"affected_nodes": 1})]
    transport = ogh.PooledTransport("http://host:8080")
    transport.mutate("council", "q", {}, None)
    before = len(_fake_http.created)
    _fake_http.script = [http.client.RemoteDisconnected("closed"), ok({})]

    outcome = transport.mutate("council", "q", {}, None)

    assert outcome.kind == ogh.FATAL
    assert len(_fake_http.created) == before, "must not open a second connection"


# ── pooling ──────────────────────────────────────────────────────────


def test_connection_is_reused_across_calls(_fake_http):
    """The entire point: 913 reads must not be 913 TCP handshakes."""
    _fake_http.script = [ok({"rows": []}) for _ in range(3)]
    transport = ogh.PooledTransport("http://host:8080")

    for _ in range(3):
        transport.query("council", "q", {}, None)

    assert len(_fake_http.created) == 1
    assert len(_fake_http.created[0].requests) == 3


def test_an_idle_connection_is_reopened_rather_than_reused(_fake_http, monkeypatch):
    """Past the idle threshold a connection is replaced, so a server that has
    since dropped it fails during connect() — where the failure is provably
    pre-send — instead of ambiguously at send time."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(ogh.time, "monotonic", lambda: clock["t"])
    _fake_http.script = [ok({"rows": []}), ok({"rows": []})]
    transport = ogh.PooledTransport("http://host:8080")

    transport.query("council", "q", {}, None)
    clock["t"] += ogh.IDLE_REUSE_MAX_SECONDS + 1
    transport.query("council", "q", {}, None)

    assert len(_fake_http.created) == 2
    assert _fake_http.created[0].closed


def test_a_recently_used_connection_is_kept(_fake_http, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(ogh.time, "monotonic", lambda: clock["t"])
    _fake_http.script = [ok({"rows": []}), ok({"rows": []})]
    transport = ogh.PooledTransport("http://host:8080")

    transport.query("council", "q", {}, None)
    clock["t"] += ogh.IDLE_REUSE_MAX_SECONDS / 2
    transport.query("council", "q", {}, None)

    assert len(_fake_http.created) == 1


def test_connection_close_response_discards_the_connection(_fake_http):
    """Honouring `Connection: close` — reusing a socket the server said it was
    closing is the stale-keepalive bug, self-inflicted."""
    _fake_http.script = [ok({"rows": []}, will_close=True), ok({"rows": []})]
    transport = ogh.PooledTransport("http://host:8080")

    transport.query("council", "q", {}, None)
    assert _fake_http.created[0].closed
    transport.query("council", "q", {}, None)
    assert len(_fake_http.created) == 2


def test_each_thread_gets_its_own_connection(_fake_http):
    """http.client connections are not thread-safe, and a mutex would serialize
    exactly the concurrent reads the server handles well (p50 5.1 → 10.7ms from
    1 to 8 workers while throughput rises ~4x)."""
    _fake_http.script = [ok({"rows": []}) for _ in range(8)]
    transport = ogh.PooledTransport("http://host:8080")
    barrier = threading.Barrier(4)

    def call():
        barrier.wait()
        transport.query("council", "q", {}, None)

    threads = [threading.Thread(target=call) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(_fake_http.created) == 4
    assert len({c.thread for c in _fake_http.created}) == 4


def test_https_url_uses_a_tls_connection(monkeypatch):
    seen = {}

    class TLSConnection(FakeConnection):
        def __init__(self, host, port=None, timeout=None, context=None):
            seen["context"] = context
            super().__init__(host, port, timeout, context)

    monkeypatch.setattr(ogh.http.client, "HTTPSConnection", TLSConnection)
    FakeConnection.script = [ok({"rows": []})]
    ogh.PooledTransport("https://graph.example").query("council", "q", {}, None)

    assert seen["context"] is not None


def test_non_http_scheme_is_rejected():
    with pytest.raises(ValueError, match="http"):
        ogh.PooledTransport("s3://bucket/graph")


# ── the request itself ───────────────────────────────────────────────


def test_query_posts_the_documented_body(_fake_http):
    _fake_http.script = [ok({"rows": []})]
    transport = ogh.PooledTransport("http://host:8080")

    transport.query("council", QUERY_SOURCE, {"slug": "a"}, "tok")

    request = _fake_http.created[0].requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/graphs/council/query"
    assert json.loads(request["body"]) == {
        "query": QUERY_SOURCE,
        "params": {"slug": "a"},
    }


def test_mutate_targets_the_mutate_endpoint(_fake_http):
    _fake_http.script = [ok({"affected_nodes": 1})]
    ogh.PooledTransport("http://host:8080").mutate("council", "q", {}, None)

    assert _fake_http.created[0].requests[0]["path"] == "/graphs/council/mutate"


def test_token_is_sent_as_a_bearer_header(_fake_http):
    _fake_http.script = [ok({"rows": []})]
    ogh.PooledTransport("http://host:8080").query("council", "q", {}, "secret-token")

    headers = _fake_http.created[0].requests[0]["headers"]
    assert headers["Authorization"] == "Bearer secret-token"


def test_no_token_sends_no_authorization_header(_fake_http):
    _fake_http.script = [ok({"rows": []})]
    ogh.PooledTransport("http://host:8080").query("council", "q", {}, None)

    assert "Authorization" not in _fake_http.created[0].requests[0]["headers"]


# ── the HTTP API contract header (omnigraph 0.12+) ───────────────────


@pytest.fixture
def contract_required(monkeypatch):
    """The state after the pin reaches format 14: an unstamped response is a
    violation. Until then a 0.11 server legitimately sends none."""
    monkeypatch.setattr(ogh, "RESPONSE_CONTRACT_REQUIRED", True)


def test_every_request_carries_the_contract_header(_fake_http):
    _fake_http.script = [ok({"rows": []}), ok({}), ok({"graphs": []})]
    transport = ogh.PooledTransport("http://host:8080")
    transport.query("council", "q", {}, "t")
    transport.mutate("council", "q", {}, "t")
    transport.graphs("t")

    sent = [r["headers"] for r in _fake_http.created[0].requests]
    assert [h[ogh.HTTP_API_CONTRACT_HEADER] for h in sent] == ["0.13"] * 3


def test_an_unstamped_response_is_accepted_from_a_0_11_server(_fake_http, monkeypatch):
    monkeypatch.setattr(ogh, "RESPONSE_CONTRACT_REQUIRED", False)
    _fake_http.script = [ok({"rows": []}, stamped=False)]

    outcome = ogh.PooledTransport("http://host:8080").query("council", "q", {}, None)

    assert outcome.kind == ogh.OK


@pytest.mark.parametrize("values", ["0.12", ["0.13", "0.13"], "0.13, 0.13"])
def test_a_response_on_another_contract_is_terminal_even_for_a_read(_fake_http, values):
    """Present but wrong, which no pin gates: the body is not decoded."""
    _fake_http.script = [
        ok(
            {"rows": [{"m.slug": "x"}]},
            stamped=False,
            headers={ogh.HTTP_API_CONTRACT_HEADER: values},
        )
    ]

    outcome = ogh.PooledTransport("http://host:8080").query("council", "q", {}, None)

    assert outcome.kind == ogh.CONTRACT_VIOLATION
    assert outcome.body == ""
    assert "effects are unknown" in outcome.error


@pytest.mark.parametrize("idempotent", [True, False])
def test_a_refusal_on_another_contract_is_not_classified_by_its_status(
    _fake_http, idempotent
):
    """A 503 stamped 0.14 is not our server asking us to wait."""
    _fake_http.script = [
        FakeResponse(
            503,
            "loading",
            stamped=False,
            headers={ogh.HTTP_API_CONTRACT_HEADER: "0.14"},
        )
    ]
    transport = ogh.PooledTransport("http://host:8080")

    outcome = transport.post("/graphs/council/query", {}, None, idempotent=idempotent)

    assert outcome.kind == ogh.CONTRACT_VIOLATION


def test_an_unstamped_success_is_not_a_result(_fake_http, contract_required):
    _fake_http.script = [ok({"rows": []}, stamped=False)]

    outcome = ogh.PooledTransport("http://host:8080").query("council", "q", {}, None)

    assert outcome.kind == ogh.CONTRACT_VIOLATION
    assert "found none" in outcome.error


@pytest.mark.parametrize("status", [409, 429, 502, 503])
def test_an_unstamped_refusal_of_a_write_is_never_retryable(
    _fake_http, contract_required, status
):
    """Each of these is retried when the server says it, because the server
    saying it proves nothing was applied. A proxy saying it proves nothing."""
    _fake_http.script = [FakeResponse(status, "upstream reset", stamped=False)]

    outcome = ogh.PooledTransport("http://host:8080").mutate("council", "q", {}, None)

    assert outcome.kind == ogh.CONTRACT_VIOLATION
    assert outcome.status == status
    assert "effects are unknown" in outcome.error


def test_a_stamped_refusal_of_a_write_keeps_its_classification(
    _fake_http, contract_required
):
    _fake_http.script = [err(503, "graph is loading", code="graph_unavailable")]

    outcome = ogh.PooledTransport("http://host:8080").mutate("council", "q", {}, None)

    assert outcome.kind == ogh.UNAVAILABLE


def test_an_unstamped_failure_of_a_read_keeps_its_classification(
    _fake_http, contract_required
):
    """A proxy's 503 while the server restarts. Repeating a read is safe."""
    _fake_http.script = [FakeResponse(503, "no healthy upstream", stamped=False)]

    outcome = ogh.PooledTransport("http://host:8080").query("council", "q", {}, None)

    assert outcome.kind == ogh.UNAVAILABLE


# ── routing: what must NOT take the HTTP path ────────────────────────


def _client(monkeypatch, uri, queries_dir, **kwargs):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    return OmnigraphClient(uri, queries_dir, **kwargs)


@pytest.fixture
def queries_dir(tmp_path):
    (tmp_path / "read.gq").write_text(QUERY_SOURCE)
    return tmp_path


@pytest.mark.parametrize("uri", ["/var/lib/witan/graph.omni", "s3://bucket/graph"])
def test_local_and_s3_stores_never_use_http(monkeypatch, queries_dir, uri):
    """There is no server to talk to; the subprocess is what opens the store."""
    client = _client(monkeypatch, uri, queries_dir, graph_id="council")
    assert client._http_transport() is None


def test_a_remote_store_uses_http(monkeypatch, queries_dir):
    client = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")
    assert client._http_transport() is not None


class BranchedClient(OmnigraphClient):
    """witan-code's branched view, as far as the base client can see it."""

    def _extra_args(self, subcommand):
        return [] if subcommand == "commit" else ["--branch", "act-alice/wip"]


def _branched(monkeypatch, queries_dir) -> BranchedClient:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    return BranchedClient("http://host:8080", queries_dir, graph_id="council")


def test_a_client_injecting_an_arg_with_no_body_field_stays_on_the_subprocess(
    monkeypatch, queries_dir
):
    """THE SAFETY PROPERTY. Sending a call over HTTP without an arg the
    subclass injects would execute it somewhere else than asked, so an arg
    with no request-body field keeps the client on the CLI. The guard is on
    `_extra_args`, not on a `branch` attribute, so a future subclass arg is
    caught by the same check instead of being silently dropped."""

    class SnapshotClient(OmnigraphClient):
        def _extra_args(self, subcommand):
            return ["--branch", "act-alice/wip", "--snapshot", "abc"]

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    client = SnapshotClient("http://host:8080", queries_dir, graph_id="council")

    assert client._http_transport() is None


def test_a_branched_read_carries_its_branch_in_the_body(
    monkeypatch, queries_dir, _fake_http
):
    """witan-code's views inject `--branch <view>`. Over HTTP that is the
    body's `branch`, which the 0.13.0 and 0.14.0 servers both read; a read
    that lost it would answer from `main`."""
    _fake_http.script = [ok({"rows": []})]
    client = _branched(monkeypatch, queries_dir)

    client.read("read.gq", "find_memory", {"slug": "a"})

    (request,) = _sent()
    assert request["path"] == "/graphs/council/query"
    assert json.loads(request["body"])["branch"] == "act-alice/wip"


@pytest.mark.parametrize("if_commit", [None, "01M08E24Y"])
def test_a_branched_write_carries_its_branch_in_the_body(
    monkeypatch, tmp_path, _fake_http, if_commit
):
    """Through the group committer, and on the conditional route too."""
    _fake_http.script = [ok({"affected_nodes": 1, "affected_edges": 0})]
    client = _branched(monkeypatch, _mutation_dir(tmp_path))

    client.change("mutations.gq", "claim", {"slug": "t-1"}, if_commit=if_commit)

    (request,) = _sent()
    assert json.loads(request["body"])["branch"] == "act-alice/wip"


def test_a_branched_batch_carries_its_branch_in_the_body(
    monkeypatch, tmp_path, _fake_http
):
    monkeypatch.setenv(og.GROUP_COMMIT_ENV_VAR, "0")
    _fake_http.script = [ok({"affected_nodes": 2, "affected_edges": 0})]
    client = _branched(monkeypatch, _mutation_dir(tmp_path))

    client.change_many(
        [
            ("mutations.gq", "claim", {"slug": "t-1"}),
            ("mutations.gq", "claim", {"slug": "t-2"}),
        ]
    )

    (request,) = _sent()
    assert json.loads(request["body"])["branch"] == "act-alice/wip"


def test_two_views_never_share_a_group_commit(monkeypatch, tmp_path):
    """The lane's leader sends its OWN branch for every write on the lane, so
    two views on one lane would commit one view's writes to the other."""
    keys = []
    monkeypatch.setattr(
        og._GROUP_COMMITTER, "submit", lambda key, *a, **k: keys.append(key)
    )
    queries = _mutation_dir(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")

    # One class for both views, so the lane key's type(self) can't tell them
    # apart and only the branch selector can.
    class View(OmnigraphClient):
        def __init__(self, *args, branch, **kwargs):
            super().__init__(*args, **kwargs)
            self.branch = branch

        def _extra_args(self, subcommand):
            return ["--branch", self.branch]

    for branch in ("act-alice/wip", "act-bob/wip"):
        View("http://host:8080", queries, graph_id="council", branch=branch).change(
            "mutations.gq", "claim", {"slug": "t-1"}
        )

    assert len(set(keys)) == 2


def test_a_missing_branch_is_terminal_not_retried(monkeypatch, queries_dir, _fake_http):
    """What 0.13.0 and 0.14.0 answer for a branch that does not exist (on 0.8.1
    it was a 500). Waiting cannot create the branch, so one attempt."""
    monkeypatch.setattr(og.time, "sleep", lambda *_: None)
    _fake_http.script = [err(404, "branch 'act-alice/wip' not found", "not_found")]
    client = _branched(monkeypatch, queries_dir)

    with pytest.raises(RuntimeError, match="branch 'act-alice/wip' not found"):
        client.read("read.gq", "find_memory", {"slug": "a"})
    assert len(_sent()) == 1


def test_commit_list_asks_the_server_for_the_branch(
    monkeypatch, queries_dir, _fake_http
):
    commits = [{"graph_commit_id": "c1", "graph_branch": "act-alice/wip"}]
    _fake_http.script = [ok({"commits": commits})]
    client = _branched(monkeypatch, queries_dir)

    assert client.commit_list("act-bob/other") == commits

    (request,) = _sent()
    assert request["method"] == "GET"
    assert request["path"] == "/graphs/council/commits?branch=act-bob%2Fother"


def test_commit_list_with_an_injected_commit_arg_stays_on_the_subprocess(
    monkeypatch, queries_dir, _fake_http
):
    """The commits route has no field for it, so it is not dropped there."""

    class SnapshotClient(OmnigraphClient):
        def _extra_args(self, subcommand):
            return ["--snapshot", "s1"] if subcommand == "commit" else []

    ran = []
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    monkeypatch.setattr(
        SnapshotClient, "_run", lambda self, *args, **kw: ran.append(args) or "[]"
    )
    client = SnapshotClient("http://host:8080", queries_dir, graph_id="council")

    assert client.commit_list("main") == []
    assert ran == [("commit", "list", "--branch", "main", "--json")]
    assert _sent() == []


@pytest.mark.parametrize(
    ("body", "match"),
    [("warning: something\n", "non-JSON"), ('{"oops": 1}', "no commit array")],
)
def test_commit_list_output_that_is_not_a_commit_array_raises(
    monkeypatch, queries_dir, _fake_http, body, match
):
    _fake_http.script = [FakeResponse(200, body)]
    client = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")

    with pytest.raises(RuntimeError, match=match):
        client.commit_list("act-alice/wip")


def test_a_branched_client_sends_no_branch_with_a_statement(
    _fake_http, monkeypatch, queries_dir
):
    """...and the one call that must NOT carry it.

    A GQ branch statement takes no `--branch` on either transport (the CLI's
    own help says "no name, params, --branch or --snapshot"), and the server
    refuses one sent with a `branch` field, so `statement` opts out of the
    injected args. Driven end to end: what matters is that the statement
    reaches the wire, at the right route, with no trace of the `--branch` this
    client sends with everything else.
    """
    FakeConnection.script = [ok({"outcome": {"kind": "created", "name": "x"}})]
    client = _branched(monkeypatch, queries_dir)

    client.statement("mutate", 'branch create "act-alice/wip" from main')

    request = FakeConnection.created[-1].requests[-1]
    assert request["path"] == "/graphs/council/mutate"
    assert json.loads(request["body"]) == {
        "query": 'branch create "act-alice/wip" from main'
    }


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE"])
def test_the_escape_hatch_forces_the_subprocess(monkeypatch, queries_dir, value):
    monkeypatch.setenv(og.HTTP_TRANSPORT_ENV_VAR, value)
    client = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")

    assert client._http_transport() is None


def test_transports_are_shared_across_clients(monkeypatch, queries_dir):
    """witan builds a fresh client per request so per-actor tokens cannot race
    (ADR-0004). If the pool were owned by the client it would be discarded every
    call and nothing would ever be reused — the change would measure as no
    faster than the subprocess it replaced."""
    first = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")
    second = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")

    assert first._http_transport() is second._http_transport()


def test_different_servers_get_different_transports(monkeypatch, queries_dir):
    first = _client(monkeypatch, "http://a:8080", queries_dir, graph_id="council")
    second = _client(monkeypatch, "http://b:8080", queries_dir, graph_id="council")

    assert first._http_transport() is not second._http_transport()


# ── end-to-end through the client, over the fake socket ──────────────


def test_read_parses_rows_and_strips_alias_prefixes(
    monkeypatch, queries_dir, _fake_http
):
    """The server returns the same body the CLI printed on stdout, so `read`'s
    parsing is shared verbatim rather than forked per transport."""
    _fake_http.script = [
        ok({"rows": [{"m.slug": "a", "m.title": "A"}], "columns": [], "row_count": 1})
    ]
    client = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")

    rows = client.read("read.gq", "find_memory", {"slug": "a"})

    assert rows == [{"slug": "a", "title": "A"}]


def test_read_restores_null_fields_omnigraph_0_11_omits(
    monkeypatch, queries_dir, _fake_http
):
    """0.11 drops null-valued fields from a row but still lists every projection
    in `columns`; callers index rows by field, so the absent ones come back as
    None rather than as a KeyError."""
    _fake_http.script = [
        ok(
            {
                "rows": [{"m.slug": "a"}],
                "columns": ["m.slug", "m.title", "m.repo"],
                "row_count": 1,
            }
        )
    ]
    client = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")

    rows = client.read("read.gq", "find_memory", {"slug": "a"})

    assert rows == [{"slug": "a", "title": None, "repo": None}]


def test_read_sends_only_the_named_query(monkeypatch, tmp_path, _fake_http):
    """The HTTP body has no query-name field, so it must carry exactly the one
    query being run — otherwise which query executes depends on how the server
    resolves a multi-query source."""
    (tmp_path / "read.gq").write_text(
        QUERY_SOURCE + "\nquery other($x: String) {\n    return $x\n}\n"
    )
    _fake_http.script = [ok({"rows": []})]
    client = _client(monkeypatch, "http://host:8080", tmp_path, graph_id="council")

    client.read("read.gq", "find_memory", {"slug": "a"})

    sent = json.loads(_fake_http.created[0].requests[0]["body"])["query"]
    assert "find_memory" in sent
    assert "other" not in sent


def test_change_posts_to_mutate(monkeypatch, tmp_path, _fake_http):
    (tmp_path / "mutations.gq").write_text(
        "query insert_memory($slug: String) {\n    insert Memory { slug: $slug }\n}\n"
    )
    _fake_http.script = [ok({"affected_nodes": 1, "affected_edges": 0})]
    client = _client(monkeypatch, "http://host:8080", tmp_path, graph_id="council")

    client.change("mutations.gq", "insert_memory", {"slug": "a"})

    request = _fake_http.created[0].requests[0]
    assert request["path"] == "/graphs/council/mutate"
    assert json.loads(request["body"])["params"] == {"slug": "a"}


# ── conditional writes (upstream #470) ───────────────────────────────


def test_if_commit_selects_the_conditional_route_and_sends_the_header(
    monkeypatch, tmp_path, _fake_http
):
    """Route and header move together, because #470 requires exactly that.

    The ordinary `/mutate` REJECTS the header rather than ignoring it, and an
    old server 404s the conditional route before executing — both of which only
    protect us if we never send one without the other.
    """
    _fake_http.script = [ok({"affected_nodes": 1, "affected_edges": 0})]
    client = _client(
        monkeypatch, "http://host:8080", _mutation_dir(tmp_path), graph_id="council"
    )

    client.change("mutations.gq", "claim", {"slug": "t-1"}, if_commit="01M08E24Y")

    request = _fake_http.created[0].requests[0]
    assert request["path"] == "/graphs/council/mutate/if-graph-commit"
    assert request["headers"][ogh.IF_GRAPH_COMMIT_HEADER] == "01M08E24Y"


def test_an_ordinary_write_sends_no_precondition_header(
    monkeypatch, tmp_path, _fake_http
):
    """★ THE SAFETY DIRECTION. A write that did not ask for a precondition must
    reach the plain route with no header — otherwise every existing caller
    silently acquires a whole-branch precondition and starts failing under any
    concurrent write at all."""
    _fake_http.script = [ok({"affected_nodes": 1, "affected_edges": 0})]
    client = _client(
        monkeypatch, "http://host:8080", _mutation_dir(tmp_path), graph_id="council"
    )

    client.change("mutations.gq", "claim", {"slug": "t-1"})

    request = _fake_http.created[0].requests[0]
    assert request["path"] == "/graphs/council/mutate"
    assert ogh.IF_GRAPH_COMMIT_HEADER not in request["headers"]


def test_read_with_commit_returns_the_snapshot_token(monkeypatch, tmp_path, _fake_http):
    """The token has to come back with the rows it belongs to.

    A head fetched separately is stale by construction, so the pairing is the
    whole point — `_update_task` fences the interval between THIS read and its
    own write.
    """
    (tmp_path / "read.gq").write_text(QUERY_SOURCE)
    _fake_http.script = [
        ok({"rows": [{"m.slug": "a"}], "graph_commit_id": "01M08E24Y"})
    ]
    client = _client(monkeypatch, "http://host:8080", tmp_path, graph_id="council")

    rows, commit = client.read_with_commit("read.gq", "find_memory", {"slug": "a"})

    assert rows == [{"slug": "a"}]
    assert commit == "01M08E24Y"


def test_read_with_commit_tolerates_a_server_that_supplies_none(
    monkeypatch, tmp_path, _fake_http
):
    """A response without the field (a read whose branch has no head commit)
    must be a clean `None` rather than a KeyError. Callers refuse to write
    conditionally on it; they do not get to pass it on as a precondition."""
    (tmp_path / "read.gq").write_text(QUERY_SOURCE)
    _fake_http.script = [ok({"rows": [{"m.slug": "a"}]})]
    client = _client(monkeypatch, "http://host:8080", tmp_path, graph_id="council")

    rows, commit = client.read_with_commit("read.gq", "find_memory", {"slug": "a"})

    assert rows == [{"slug": "a"}]
    assert commit is None


def test_change_returns_the_commit_its_own_write_produced(
    monkeypatch, tmp_path, _fake_http
):
    """`change()`'s return value is the NEW head, not the one fenced on. Shape
    verified against the real CLI, 2026-08-18: `omnigraph mutate --json` on a
    real store answers exactly `{"commit": {"graph_commit_id": ..., ...}}`,
    and the HTTP `ChangeOutput` struct is the same type serialized either way.
    """
    _fake_http.script = [
        ok(
            {
                "branch": "main",
                "query_name": "claim",
                "affected_nodes": 1,
                "affected_edges": 0,
                "actor_id": None,
                "commit": {"graph_commit_id": "01AFTER", "manifest_version": 2},
            }
        )
    ]
    client = _client(
        monkeypatch, "http://host:8080", _mutation_dir(tmp_path), graph_id="council"
    )

    new_commit = client.change("mutations.gq", "claim", {"slug": "t-1"})

    assert new_commit == "01AFTER"


def test_change_returns_none_when_the_tier_supplies_no_commit(
    monkeypatch, tmp_path, _fake_http
):
    """A response that omits `commit` gives `None`, not a KeyError, the same
    posture as `read_with_commit`. 0.13 sends `"commit": null` for a write
    that matched nothing."""
    _fake_http.script = [
        ok({"branch": "main", "query_name": "claim", "affected_nodes": 1})
    ]
    client = _client(
        monkeypatch, "http://host:8080", _mutation_dir(tmp_path), graph_id="council"
    )

    assert client.change("mutations.gq", "claim", {"slug": "t-1"}) is None


# ── the seam: a real 409 response all the way out to OmnigraphConflict ───

# What omnigraph-server actually returns to a racing writer, captured from a QA
# `task_claim` race on 2026-08-17.
_WRITE_AUTHORITY_409 = (
    "write authority 'graph_head:main' changed during preparation "
    "(expected 01M08E24Y2WWC3QVE9MD3K6CWN, current 01M08E27K5J56HF8QZ7GX61X3F) "
    "— reprepare from the current branch state"
)


def _mutation_dir(tmp_path):
    (tmp_path / "mutations.gq").write_text(
        "query claim($slug: String) {\n    insert Task { slug: $slug }\n}\n"
    )
    return tmp_path


def test_a_409_over_http_surfaces_as_a_conflict_to_a_cas_caller(
    monkeypatch, tmp_path, _fake_http
):
    """★ THE TEST THE DEFECT SLIPPED THROUGH, and the reason it is here rather
    than beside the classifier unit tests.

    `classify_status` was tested alone, and `task_claim`'s conflict handling was
    tested alone by RAISING `OmnigraphConflict` directly (see
    `test_claim_conflict_reports_lost_race_without_clobber` in the witan suite).
    Both passed throughout. Nothing exercised the JOIN — whether a real 409
    response ever becomes that exception — so a 409 classified FATAL left every
    compare-and-swap caller unreachable while its own tests stayed green.

    This drives the whole path: HTTP 409 -> classify_status -> _retry_loop ->
    OmnigraphConflict, over the transport the DEPLOYED service actually uses.
    """
    _fake_http.script = [err(409, _WRITE_AUTHORITY_409, code="conflict")]
    client = _client(
        monkeypatch, "http://host:8080", _mutation_dir(tmp_path), graph_id="council"
    )

    with pytest.raises(OmnigraphConflict, match="write authority"):
        client.change("mutations.gq", "claim", {"slug": "t-1"}, surface_conflict=True)

    # ONE attempt. A retry would re-apply the claim over whoever won the race,
    # which is the entire reason `surface_conflict` exists.
    assert len(_fake_http.created[0].requests) == 1


def test_head_drift_over_http_is_terminal(monkeypatch, tmp_path, _fake_http):
    """The HTTP join for drift: a 409 naming it is not the retryable 409.
    One scripted response, so a second attempt would exhaust the script."""
    _fake_http.script = [
        err(409, "head is ahead of manifest, run omnigraph repair", code="conflict")
    ]
    client = _client(
        monkeypatch, "http://host:8080", _mutation_dir(tmp_path), graph_id="council"
    )

    with pytest.raises(RuntimeError, match="Retrying will not clear it"):
        client.change("mutations.gq", "claim", {"slug": "t-1"})

    assert len(_fake_http.created[0].requests) == 1


def test_a_412_is_terminal_for_an_ordinary_writer(monkeypatch, tmp_path, _fake_http):
    """★ THE ASYMMETRY THAT MATTERS, and the reason this asserts a COUNT.

    The 409 test below proves a conflict is retried. This proves a precondition
    failure is NOT — one attempt, then a hard error. Retrying it would re-send a
    write the server has explicitly told us it will never replay, over whoever
    won the race.

    Scripting only ONE response is itself part of the assertion: a second
    attempt would exhaust the script and blow up differently.
    """
    _fake_http.script = [err(412, "precondition_failure", code="precondition_failed")]
    client = _client(
        monkeypatch, "http://host:8080", _mutation_dir(tmp_path), graph_id="council"
    )

    with pytest.raises(RuntimeError, match="must not be retried"):
        client.change("mutations.gq", "claim", {"slug": "t-1"})

    assert len(_fake_http.created[0].requests) == 1


def test_a_412_surfaces_as_a_conflict_to_a_cas_caller(
    monkeypatch, tmp_path, _fake_http
):
    """`task_claim` catches `OmnigraphConflict` to re-read and answer lost_race.

    A conditional claim that loses must reach that handler, not a bare
    RuntimeError — same destination as the 409 path, reached from a status that
    must never be retried.
    """
    _fake_http.script = [err(412, "precondition_failure", code="precondition_failed")]
    client = _client(
        monkeypatch, "http://host:8080", _mutation_dir(tmp_path), graph_id="council"
    )

    with pytest.raises(OmnigraphConflict):
        client.change("mutations.gq", "claim", {"slug": "t-1"}, surface_conflict=True)

    assert len(_fake_http.created[0].requests) == 1


def test_recovery_required_on_a_write_is_indeterminate_and_terminal(
    monkeypatch, tmp_path, _fake_http
):
    """★ ONE SIGNAL COVERS TWO CONDITIONS, SO THE WRITE STOPS.

    `recovery_required` is both an effect-free bystander barrier (measured: six
    concurrent appends to DISTINCT keys, five losers, a write immediately after
    succeeds) AND a request whose table effects may already have landed
    (upstream #470). Nothing on the wire separates them.

    An earlier version of this code retried it on a short budget. That was
    wrong, and shortening the budget was the tell: it narrows the window in
    which a duplicate is created after recovery rolls the original forward
    rather than closing it. Scripting only ONE response is part of the
    assertion — a retry would exhaust the script.
    """
    monkeypatch.setattr(og.time, "sleep", lambda *_: None)
    _fake_http.script = [
        err(503, "recovery required for operation 01KZY…: pending Load recovery")
    ]
    client = _client(
        monkeypatch, "http://host:8080", _mutation_dir(tmp_path), graph_id="council"
    )

    with pytest.raises(og.WriteIndeterminate, match="INDETERMINATE"):
        client.change("mutations.gq", "claim", {"slug": "t-1"})

    assert len(_fake_http.created[0].requests) == 1


def test_an_unstamped_refusal_of_a_write_is_indeterminate_and_terminal(
    monkeypatch, tmp_path, _fake_http, contract_required
):
    """One response scripted: a retry would exhaust the script. Raised as
    WriteIndeterminate so a caller that re-reads on that does so here too."""
    monkeypatch.setattr(og.time, "sleep", lambda *_: None)
    _fake_http.script = [FakeResponse(503, "no healthy upstream", stamped=False)]
    client = _client(
        monkeypatch, "http://host:8080", _mutation_dir(tmp_path), graph_id="council"
    )

    with pytest.raises(og.WriteIndeterminate, match="contract stamp"):
        client.change("mutations.gq", "claim", {"slug": "t-1"})

    assert len(_fake_http.created[0].requests) == 1


def test_an_unstamped_success_of_a_read_is_an_error_not_rows(
    monkeypatch, queries_dir, _fake_http, contract_required
):
    _fake_http.script = [ok({"rows": [{"m.slug": "x"}]}, stamped=False)]
    client = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")

    with pytest.raises(RuntimeError, match="effects are unknown"):
        client.read("read.gq", "find_memory", {"slug": "x"})

    assert len(_fake_http.created[0].requests) == 1


def test_recovery_required_on_a_write_is_never_a_lost_race(
    monkeypatch, tmp_path, _fake_http
):
    """★ AND IT IS STILL NOT A CONFLICT, even though it is now terminal.

    The barrier fires for writers contending with nobody, so a CAS caller must
    NOT receive `OmnigraphConflict` here — `task_claim` would answer
    `lost_race` to someone who never raced anyone, which is a confident wrong
    answer and worse than an error. It gets the indeterminate error like any
    other writer.
    """
    monkeypatch.setattr(og.time, "sleep", lambda *_: None)
    _fake_http.script = [
        err(503, "recovery required for operation 01KZY…: pending Load recovery")
    ]
    client = _client(
        monkeypatch, "http://host:8080", _mutation_dir(tmp_path), graph_id="council"
    )

    with pytest.raises(og.WriteIndeterminate):
        client.change("mutations.gq", "claim", {"slug": "t-1"}, surface_conflict=True)


def test_recovery_required_on_a_read_still_retries(monkeypatch, tmp_path, _fake_http):
    """A read cannot duplicate anything, and the barrier does clear itself.

    ★ `connect_retry=False` IS WHAT MAKES THIS DISCRIMINATE, and the first draft
    did not. Classified UNAVAILABLE (the old behaviour) a 503 ALSO retries, so a
    plain "did it try twice?" passed with and without the change. With
    connect_retry off, UNAVAILABLE degrades to FATAL and raises on attempt 1,
    while RECOVERY_REQUIRED keeps its budget — so the two diverge.
    """
    monkeypatch.setattr(og.time, "sleep", lambda *_: None)
    (tmp_path / "read.gq").write_text(QUERY_SOURCE)
    _fake_http.script = [
        err(503, "recovery required for operation 01KZY…: pending Load recovery"),
        ok({"rows": [{"m.slug": "a"}]}),
    ]
    client = _client(
        monkeypatch,
        "http://host:8080",
        tmp_path,
        graph_id="council",
        connect_retry=False,
    )

    assert client.read("read.gq", "find_memory", {"slug": "a"}) == [{"slug": "a"}]
    assert len(_fake_http.created[0].requests) == 2


def test_a_409_over_http_is_retried_for_an_ordinary_writer(
    monkeypatch, tmp_path, _fake_http
):
    """The non-CAS half, which is the larger blast radius.

    Every plain `memory_store` that merely lost a race died outright where one
    retry would have committed. Classified retryable, the second attempt lands.
    """
    monkeypatch.setattr(og.time, "sleep", lambda *_: None)
    _fake_http.script = [
        err(409, _WRITE_AUTHORITY_409, code="conflict"),
        ok({"affected_nodes": 1, "affected_edges": 0}),
    ]
    client = _client(
        monkeypatch, "http://host:8080", _mutation_dir(tmp_path), graph_id="council"
    )

    client.change("mutations.gq", "claim", {"slug": "t-1"})

    assert len(_fake_http.created[0].requests) == 2


def test_change_many_sends_one_composed_request(monkeypatch, tmp_path, _fake_http):
    """One commit per batch is the property that keeps the store from
    fragmenting; the transport must not undo it by splitting the batch."""
    (tmp_path / "mutations.gq").write_text(
        "query insert_topic($slug: String) {\n    insert Topic { slug: $slug }\n}\n"
    )
    _fake_http.script = [ok({"affected_nodes": 2})]
    client = _client(monkeypatch, "http://host:8080", tmp_path, graph_id="council")

    client.change_many(
        [
            ("mutations.gq", "insert_topic", {"slug": "a"}),
            ("mutations.gq", "insert_topic", {"slug": "b"}),
        ]
    )

    assert len(_fake_http.created[0].requests) == 1
    body = json.loads(_fake_http.created[0].requests[0]["body"])
    assert body["params"] == {"s0_slug": "a", "s1_slug": "b"}


def test_change_many_fences_the_whole_batch(monkeypatch, tmp_path, _fake_http):
    """A batch with `if_commit` is one conditional request: the route and the
    header move together, exactly as for `change`."""
    (tmp_path / "mutations.gq").write_text(
        "query insert_topic($slug: String) {\n    insert Topic { slug: $slug }\n}\n"
    )
    _fake_http.script = [ok({"affected_nodes": 2})]
    client = _client(monkeypatch, "http://host:8080", tmp_path, graph_id="council")

    client.change_many(
        [
            ("mutations.gq", "insert_topic", {"slug": "a"}),
            ("mutations.gq", "insert_topic", {"slug": "b"}),
        ],
        if_commit="01M08E24Y",
    )

    [request] = _fake_http.created[0].requests
    assert request["path"] == "/graphs/council/mutate/if-graph-commit"
    assert request["headers"][ogh.IF_GRAPH_COMMIT_HEADER] == "01M08E24Y"


def test_a_single_step_batch_keeps_its_fence(monkeypatch, tmp_path, _fake_http):
    """One step goes straight to `change`; the precondition must go with it."""
    _fake_http.script = [ok({"affected_nodes": 1})]
    client = _client(
        monkeypatch, "http://host:8080", _mutation_dir(tmp_path), graph_id="council"
    )

    client.change_many([("mutations.gq", "claim", {"slug": "t-1"})], if_commit="01X")

    [request] = _fake_http.created[0].requests
    assert request["path"] == "/graphs/council/mutate/if-graph-commit"
    assert request["headers"][ogh.IF_GRAPH_COMMIT_HEADER] == "01X"


def test_a_chunked_batch_cannot_be_fenced(monkeypatch, tmp_path, _fake_http):
    """Chunks are separate commits, so only the first could state the head it
    was read at. Refused before anything is sent."""
    client = _client(
        monkeypatch, "http://host:8080", _mutation_dir(tmp_path), graph_id="council"
    )

    with pytest.raises(ValueError, match="chunked"):
        client.change_many(
            [("mutations.gq", "claim", {"slug": "t-1"})] * 3,
            chunk_size=2,
            if_commit="01X",
        )

    assert not _fake_http.created


def test_the_guard_still_rewrites_params_over_http(monkeypatch, tmp_path, _fake_http):
    """The write guard redacts secrets before they are persisted. It runs in
    `change`, above the transport split — this pins that it did not end up on
    one side of it."""
    (tmp_path / "mutations.gq").write_text(
        "query insert_memory($slug: String) {\n    insert Memory { slug: $slug }\n}\n"
    )
    _fake_http.script = [ok({"affected_nodes": 1})]
    client = _client(
        monkeypatch,
        "http://host:8080",
        tmp_path,
        graph_id="council",
        guard=lambda name, params: {**params, "slug": "REDACTED"},
    )

    client.change("mutations.gq", "insert_memory", {"slug": "secret"})

    body = json.loads(_fake_http.created[0].requests[0]["body"])
    assert body["params"]["slug"] == "REDACTED"


# ── the shared retry policy, reached over HTTP ───────────────────────


def test_a_429_takes_the_admission_cap_backoff(monkeypatch, queries_dir, _fake_http):
    """The cap has its own budget and does not consume _MAX_ATTEMPTS. Over HTTP
    it arrives as a status rather than as stderr text, and must land on the same
    branch."""
    slept = []
    monkeypatch.setattr(og.time, "sleep", slept.append)
    _fake_http.script = [
        err(429, "in-flight count cap exceeded"),
        err(429, "in-flight count cap exceeded"),
        ok({"rows": []}),
    ]
    client = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")

    assert client.read("read.gq", "find_memory", {"slug": "a"}) == []
    assert len(slept) == 2


def test_a_conflict_is_surfaced_to_a_cas_caller(monkeypatch, tmp_path, _fake_http):
    """task_claim must be able to LOSE a race rather than clobber the winner."""
    (tmp_path / "mutations.gq").write_text(
        "query claim($slug: String) {\n    insert Task { slug: $slug }\n}\n"
    )
    _fake_http.script = [err(409, "stale view; refresh and retry")]
    client = _client(monkeypatch, "http://host:8080", tmp_path, graph_id="council")

    with pytest.raises(OmnigraphConflict):
        client.change("mutations.gq", "claim", {"slug": "a"}, surface_conflict=True)


def test_a_conflict_is_retried_when_not_surfaced(monkeypatch, tmp_path, _fake_http):
    (tmp_path / "mutations.gq").write_text(
        "query upsert($slug: String) {\n    insert Memory { slug: $slug }\n}\n"
    )
    monkeypatch.setattr(og.time, "sleep", lambda _: None)
    _fake_http.script = [
        err(409, "stale view; refresh and retry"),
        ok({"affected_nodes": 1}),
    ]
    client = _client(monkeypatch, "http://host:8080", tmp_path, graph_id="council")

    client.change("mutations.gq", "upsert", {"slug": "a"})

    assert not _fake_http.script


def test_a_connect_failure_rides_out_a_restart(monkeypatch, queries_dir, _fake_http):
    """The 150s budget exists because the deployed server is unreachable for
    52-61s on every restart, and restarts are routine (a token-map change is a
    restart). A connect failure over HTTP must reach that same budget."""
    monkeypatch.setattr(og.time, "sleep", lambda _: None)
    clock = {"t": 0.0}
    monkeypatch.setattr(og.time, "monotonic", lambda: clock["t"])

    calls = {"n": 0}
    real_connect = FakeConnection.connect

    def flaky_connect(self):
        calls["n"] += 1
        if calls["n"] < 4:
            clock["t"] += 10.0
            raise ConnectionRefusedError("connection refused")
        return real_connect(self)

    monkeypatch.setattr(FakeConnection, "connect", flaky_connect)
    _fake_http.script = [ok({"rows": []})]
    client = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")

    assert client.read("read.gq", "find_memory", {"slug": "a"}) == []
    assert calls["n"] == 4


def test_a_denial_is_not_retried(monkeypatch, queries_dir, _fake_http):
    """A Cedar denial is not transient — retrying it 8 times reports the same
    answer far later and burns the budget."""
    _fake_http.script = [err(403, "policy denied action 'read'", "forbidden")]
    client = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")

    with pytest.raises(RuntimeError, match="policy denied"):
        client.read("read.gq", "find_memory", {"slug": "a"})

    assert len(_fake_http.created[0].requests) == 1


def test_the_error_does_not_invent_a_cli_exit_status(
    monkeypatch, queries_dir, _fake_http
):
    """`exit N` is a subprocess concept. An HTTP failure reporting one would
    send whoever reads the log looking for a CLI invocation that never ran."""
    _fake_http.script = [err(403, "policy denied action 'read'", "forbidden")]
    client = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")

    with pytest.raises(RuntimeError) as exc:
        client.read("read.gq", "find_memory", {"slug": "a"})

    assert "exit" not in str(exc.value)
    assert "HTTP 403" in str(exc.value)


# ── token resolution on the HTTP path ────────────────────────────────


def test_an_explicit_token_is_used(monkeypatch, queries_dir, _fake_http):
    _fake_http.script = [ok({"rows": []})]
    client = _client(
        monkeypatch, "http://host:8080", queries_dir, graph_id="council", token="t"
    )

    client.read("read.gq", "find_memory", {"slug": "a"})

    headers = _fake_http.created[0].requests[0]["headers"]
    assert headers["Authorization"] == "Bearer t"


def test_an_ambient_token_is_still_honoured(monkeypatch, queries_dir, _fake_http):
    """`export OMNIGRAPH_BEARER_TOKEN=…` with nothing configured is the CLI's own
    documented fallback and a supported way to drive a remote graph. The HTTP
    path reads no environment of its own, so without an explicit fallback this
    silently became unauthenticated 401s."""
    monkeypatch.setenv("OMNIGRAPH_BEARER_TOKEN", "ambient-secret")
    _fake_http.script = [ok({"rows": []})]
    client = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")

    client.read("read.gq", "find_memory", {"slug": "a"})

    headers = _fake_http.created[0].requests[0]["headers"]
    assert headers["Authorization"] == "Bearer ambient-secret"


def test_an_explicit_token_beats_an_ambient_one(monkeypatch, queries_dir, _fake_http):
    monkeypatch.setenv("OMNIGRAPH_BEARER_TOKEN", "ambient-secret")
    _fake_http.script = [ok({"rows": []})]
    client = _client(
        monkeypatch,
        "http://host:8080",
        queries_dir,
        graph_id="council",
        token="explicit",
    )

    client.read("read.gq", "find_memory", {"slug": "a"})

    headers = _fake_http.created[0].requests[0]["headers"]
    assert headers["Authorization"] == "Bearer explicit"


def test_two_actors_tokens_do_not_race_through_the_shared_pool(
    monkeypatch, queries_dir, _fake_http
):
    """The transport is shared process-wide but the token is per call, which is
    what makes sharing safe under ADR-0004's per-request per-actor identity. A
    token cached on the transport would leak one actor's identity onto another's
    request."""
    monkeypatch.delenv("OMNIGRAPH_BEARER_TOKEN", raising=False)
    _fake_http.script = [ok({"rows": []}), ok({"rows": []})]

    for token in ("actor-a-token", "actor-b-token"):
        _client(
            monkeypatch,
            "http://host:8080",
            queries_dir,
            graph_id="council",
            token=token,
        ).read("read.gq", "find_memory", {"slug": "a"})

    sent = [r["headers"]["Authorization"] for r in _fake_http.created[0].requests]
    assert sent == ["Bearer actor-a-token", "Bearer actor-b-token"]


# ── load and export over HTTP ────────────────────────────────────────
#
# The 0.14 CLI refuses a plain-http server target, and the deployed data tier
# is one, so a remote load or export through the subprocess stops working at
# the pin bump. These routes are the ones the 0.13 CLI itself posts to.


def _sent(fake=FakeConnection) -> list[dict]:
    """Every request sent, across every connection the transport opened."""
    return [request for conn in fake.created for request in conn.requests]


_RECORDS = [
    {"type": "Memory", "data": {"slug": "m-1", "title": "café"}},
    {"edge": "Tagged", "from": "m-1", "to": "t-1"},
]


def test_a_remote_load_posts_the_ndjson_body_to_the_load_route(
    monkeypatch, queries_dir, _fake_http
):
    """The body is byte-for-byte what `omnigraph load --data` would have read
    from its file, so the server sees the same records on either transport."""
    summary = {"branch": "main", "total_entities": 2, "actor_id": None}
    _fake_http.script = [ok(summary)]
    client = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")

    out = client.load_batch(_RECORDS, "merge")

    (request,) = _sent()
    assert request["method"] == "POST"
    assert request["path"] == "/graphs/council/load/ndjson?mode=merge"
    assert request["headers"]["Content-Type"] == "application/x-ndjson"
    assert request["body"] == ("".join(json.dumps(r) + "\n" for r in _RECORDS).encode())
    assert json.loads(out) == summary


def test_a_branched_load_carries_its_branch_as_query_parameters(
    monkeypatch, queries_dir, _fake_http
):
    """witan-code's branched view injects `--branch <view> --from main` into
    `load`. Over HTTP those are the route's `branch` and `from` parameters; a
    load that lost them would land a WIP reindex in `main`."""

    class BranchedClient(OmnigraphClient):
        def _extra_args(self, subcommand):
            if subcommand == "load":
                return ["--branch", "act-alice/wip", "--from", "main"]
            return ["--branch", "act-alice/wip"]

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    _fake_http.script = [ok({"branch": "act-alice/wip"})]
    client = BranchedClient("http://host:8080", queries_dir, graph_id="council")

    client.load_batch(_RECORDS, "merge")

    (request,) = _sent()
    assert request["path"] == (
        "/graphs/council/load/ndjson?mode=merge&branch=act-alice%2Fwip&from=main"
    )


def test_a_load_flag_with_no_query_parameter_is_refused_not_dropped(
    monkeypatch, queries_dir, _fake_http
):
    class SnapshotClient(OmnigraphClient):
        def _extra_args(self, subcommand):
            return ["--snapshot", "abc"]

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    client = SnapshotClient("http://host:8080", queries_dir, graph_id="council")

    with pytest.raises(RuntimeError, match="'--snapshot'.*refusing to drop"):
        client.load_batch(_RECORDS, "merge")
    assert _sent() == []


def test_a_load_lost_midflight_is_not_resent(monkeypatch, queries_dir, _fake_http):
    """A load whose response never arrived may have committed. Re-sending it
    is the duplicate write the transport's `idempotent=False` exists to stop."""
    monkeypatch.setattr(og.time, "sleep", lambda *_: None)
    _fake_http.script = [ConnectionResetError("reset by peer"), ok({})]
    client = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")

    with pytest.raises(RuntimeError, match="reset by peer"):
        client.load_batch(_RECORDS, "merge")
    assert len(_sent()) == 1


def test_a_load_refused_before_effects_is_retried(monkeypatch, queries_dir, _fake_http):
    """The route's documented 409 is "prepared load authority changed before
    effects": nothing was written, so the retry policy re-sends it."""
    monkeypatch.setattr(og.time, "sleep", lambda *_: None)
    _fake_http.script = [
        err(409, "prepared load authority changed before effects", "conflict"),
        ok({"total_entities": 2}),
    ]
    client = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")

    client.load_batch(_RECORDS, "merge")

    assert len(_sent()) == 2


def test_a_load_body_is_utf8_whatever_it_contains(_fake_http):
    """http.client encodes a str body as ISO-8859-1 and raises outside it, so
    the transport encodes it. witan rows are routinely non-ASCII prose."""
    _fake_http.script = [ok({})]
    transport = ogh.PooledTransport("http://host:8080")

    outcome = transport.load_ndjson("g", '{"t": "naïve ✓"}\n', None, mode="merge")

    assert outcome.kind == ogh.OK
    (request,) = _sent()
    assert request["body"] == '{"t": "naïve ✓"}\n'.encode()
    assert request["headers"]["Content-Length"] == str(len(request["body"]))


_EXPORT_BODY = '{"type":"Memory","data":{"slug":"m-1"}}\n{"edge":"Tagged"}\n'


def test_a_remote_export_streams_the_export_route_into_the_file(
    monkeypatch, queries_dir, tmp_path, _fake_http
):
    _fake_http.script = [FakeResponse(200, _EXPORT_BODY)]
    client = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")
    out = tmp_path / "export.jsonl"

    client.export_to(out)

    (request,) = _sent()
    assert request["method"] == "POST"
    assert request["path"] == "/graphs/council/export"
    assert json.loads(request["body"]) == {"type_names": []}
    assert request["headers"]["Accept"] == "application/x-ndjson"
    assert out.read_text() == _EXPORT_BODY


def test_an_export_cut_off_midstream_is_retried_from_an_empty_file(
    monkeypatch, queries_dir, tmp_path, _fake_http
):
    """omnigraph aborts the chunked body of an export it cannot finish, since
    the 200 is already sent. That is a read failing mid-flight, so it is
    retried, and the retry must overwrite the prefix rather than append to it."""
    monkeypatch.setattr(og.time, "sleep", lambda *_: None)
    _fake_http.script = [
        FakeResponse(
            200,
            '{"type":"Memory","data":{"slug":"ha',
            fail_after_body=http.client.IncompleteRead(b""),
        ),
        FakeResponse(200, _EXPORT_BODY),
    ]
    client = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")
    out = tmp_path / "export.jsonl"

    client.export_to(out, label="export (target)")

    assert len(_sent()) == 2
    assert out.read_text() == _EXPORT_BODY
    assert FakeConnection.created[0].closed  # the cut-off connection is not reused


def test_a_sized_export_cut_short_is_retried_not_kept(
    monkeypatch, queries_dir, tmp_path, _fake_http
):
    """A real HTTPResponse with Content-Length reads b"" at a premature EOF
    instead of raising, which looks exactly like the end of the body. Kept, the
    truncated file would reach merge reconciliation as the whole graph."""

    class _Socket:
        def __init__(self, data: bytes):
            self._file = io.BytesIO(data)

        def makefile(self, mode):
            return self._file

    head = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/x-ndjson\r\n"
        f"Content-Length: {len(_EXPORT_BODY)}\r\n"
        f"{ogh.HTTP_API_CONTRACT_HEADER}: {ogh.HTTP_API_CONTRACT}\r\n\r\n"
    )
    truncated = http.client.HTTPResponse(
        _Socket(head.encode() + _EXPORT_BODY.encode()[:10])
    )
    truncated.begin()
    monkeypatch.setattr(og.time, "sleep", lambda *_: None)
    _fake_http.script = [truncated, FakeResponse(200, _EXPORT_BODY)]
    client = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")
    out = tmp_path / "export.jsonl"

    client.export_to(out)

    assert len(_sent()) == 2
    assert out.read_text() == _EXPORT_BODY


def test_an_export_refusal_is_raised_with_the_servers_message(
    monkeypatch, queries_dir, tmp_path, _fake_http
):
    _fake_http.script = [err(403, "policy denied action 'export'", "forbidden")]
    client = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")

    with pytest.raises(
        RuntimeError,
        match=r"(?s)export \(target\) failed.*policy denied action 'export'",
    ):
        client.export_to(tmp_path / "export.jsonl", label="export (target)")


def test_an_unstamped_export_is_not_streamed_into_the_file(
    monkeypatch, queries_dir, tmp_path, _fake_http, contract_required
):
    """A 200 without the contract stamp is not the server's export. Its body
    is left unread, so the connection holding it is discarded too."""
    _fake_http.script = [FakeResponse(200, _EXPORT_BODY, stamped=False)]
    client = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")
    out = tmp_path / "export.jsonl"

    with pytest.raises(RuntimeError, match="Omnigraph-Http-Api"):
        client.export_to(out)
    assert out.read_text() == ""
    assert FakeConnection.created[0].closed


def test_an_export_that_cannot_write_its_file_is_not_retried(
    monkeypatch, queries_dir, tmp_path, _fake_http
):
    """A local write failure is not a restarting server, so it must not spend
    the 150s unavailable budget before saying so."""
    _fake_http.script = [FakeResponse(200, _EXPORT_BODY)]
    client = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")

    with pytest.raises(RuntimeError, match="could not write"):
        client.export_to(tmp_path / "missing-dir" / "export.jsonl")
    assert len(_sent()) == 0


def test_a_sink_that_fails_midstream_drops_the_half_read_connection(
    monkeypatch, _fake_http
):
    """A full disk part-way through an export leaves the response unread. Kept
    in the pool, that connection would fail the thread's next request after it
    was sent, which for a mutate is an indeterminate write."""
    monkeypatch.setattr(ogh, "EXPORT_CHUNK_BYTES", 8)

    class FullDisk:
        writes = 0

        def write(self, chunk):
            self.writes += 1
            if self.writes == 2:
                raise OSError(28, "No space left on device")

    _fake_http.script = [
        ok({"rows": []}),
        FakeResponse(200, _EXPORT_BODY),
        ok({"rows": []}),
    ]
    transport = ogh.PooledTransport("http://host:8080")
    # Warm the connection, so the export reuses it and the next call would too.
    transport.query("council", "query q() { return 1 }", {}, None)

    with pytest.raises(OSError, match="No space left"):
        transport.export("council", FullDisk(), None)
    transport.query("council", "query q() { return 1 }", {}, None)

    assert [len(conn.requests) for conn in FakeConnection.created] == [2, 1]
    assert FakeConnection.created[0].closed


@pytest.mark.parametrize("uri", ["/var/lib/witan/graph.omni", "s3://bucket/graph"])
def test_a_local_load_and_export_stay_on_the_cli(
    monkeypatch, queries_dir, tmp_path, _fake_http, uri
):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd[1])
        return og.subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(og.subprocess, "run", fake_run)
    monkeypatch.setattr(og, "store_write_lock", lambda store: og.nullcontext())
    client = _client(monkeypatch, uri, queries_dir, graph_id="council")

    client.load_batch(_RECORDS, "merge")
    client.export_to(tmp_path / "export.jsonl")

    assert calls == ["load", "export"]
    assert _sent() == []


# ── against a real server (opt-in) ────────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("WITAN_TEST_OMNIGRAPH_SERVER"),
    reason="set WITAN_TEST_OMNIGRAPH_SERVER=<url> to check the HTTP path live",
)
def test_live_server_answers_the_pooled_transport(monkeypatch):
    """The only check that catches the SHAPE of the API drifting.

    Everything above drives a fake socket, so it pins our side of the contract
    and nothing else — a server that moved the endpoint, renamed the error
    envelope, or stopped accepting an inline query would pass every one of them.
    This asserts the two things the transport actually depends on: that
    `/graphs/<id>/query` is where a query goes, and that a rejected token comes
    back as a classifiable error rather than something we would read as fatal.

    Opt-in because it needs a reachable server; point it at a port-forward:
        kubectl -n omnigraph port-forward svc/omnigraph-server 18080:8080
        WITAN_TEST_OMNIGRAPH_SERVER=http://127.0.0.1:18080 pytest -k live_server

    Deliberately uses a BOGUS token: the endpoint and the error envelope are
    what is under test, and a test that needed a real credential could not run
    anywhere useful.
    """
    monkeypatch.undo()  # drop the fake-socket patches; talk to the real thing
    transport = ogh.PooledTransport(os.environ["WITAN_TEST_OMNIGRAPH_SERVER"])
    graph = os.environ.get("WITAN_TEST_OMNIGRAPH_GRAPH", "council")

    outcome = transport.query(graph, "query q() {\n    return 1\n}\n", {}, "bogus")

    assert outcome.kind != ogh.OK, "a bogus token must not be accepted"
    assert outcome.status in (401, 403), (
        f"expected an auth rejection, got HTTP {outcome.status}: {outcome.error}. "
        "A 404 here means the query endpoint moved and the transport is "
        "addressing a path the server no longer serves."
    )


# ── A timeout has to name its own budget ──────────────────────────
#
# `socket.timeout` stringifies to bare "timed out". That was the ENTIRE
# operator-facing text of the CI indexer's failure, and the reason attributing
# it took five days: "timed out" is equally consistent with a TCP connect, a
# server-side deadline and this client's own budget.


def test_a_midflight_timeout_names_the_budget_it_exceeded(_fake_http, monkeypatch):
    clock = {"t": 100.0}
    monkeypatch.setattr(ogh.time, "monotonic", lambda: clock["t"])

    def slow_request(self, method, path, body=None, headers=None):
        clock["t"] += 121.4
        raise TimeoutError("timed out")

    monkeypatch.setattr(FakeConnection, "request", slow_request)
    transport = ogh.PooledTransport("http://host:8080")

    error = transport.mutate("council", "q", {}, None).error

    assert "timed out after 121.4s" in error
    assert "client timeout budget 120s" in error
    assert "DEFAULT_TIMEOUT_SECONDS" in error


def test_a_connect_timeout_names_the_budget_too(_fake_http, monkeypatch):
    """Same message from the other side of `connect()`.

    The two failures classify differently (pre-send is retryable, mid-flight on
    a write is not) but read identically to an operator, so neither may be the
    one that stays bare.
    """
    clock = {"t": 0.0}
    monkeypatch.setattr(ogh.time, "monotonic", lambda: clock["t"])

    def slow_connect(self):
        clock["t"] += 120.0
        raise TimeoutError("timed out")

    monkeypatch.setattr(FakeConnection, "connect", slow_connect)

    error = ogh.PooledTransport("http://host:8080").query("g", "q", {}, None).error

    assert "timed out after 120.0s" in error
    assert "client timeout budget 120s" in error


def test_a_non_timeout_failure_still_reports_how_long_it_ran(_fake_http, monkeypatch):
    """Elapsed separates "died immediately" from "ran to a deadline someone else
    owns" — a server-side cutoff arrives as a reset, not as a timeout, and only
    the elapsed time tells them apart."""
    clock = {"t": 5.0}
    monkeypatch.setattr(ogh.time, "monotonic", lambda: clock["t"])

    def failing_request(self, method, path, body=None, headers=None):
        clock["t"] += 30.0
        raise ConnectionResetError("reset")

    monkeypatch.setattr(FakeConnection, "request", failing_request)

    error = ogh.PooledTransport("http://host:8080").mutate("g", "q", {}, None).error

    assert "reset (after 30.0s)" in error


def test_the_budget_reported_is_the_transports_own_not_the_default(
    _fake_http, monkeypatch
):
    """A transport constructed with a narrower budget must say so.

    Reporting the module default would be worse than saying nothing: it would
    send the reader to a constant that is not the one that fired.
    """

    def timing_out(self, method, path, body=None, headers=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(FakeConnection, "request", timing_out)

    error = (
        ogh.PooledTransport("http://host:8080", timeout=7.5)
        .query("g", "q", {}, None)
        .error
    )

    assert "client timeout budget 7.5s" in error
    # ...and does NOT name the module default, which is 120 and had nothing to
    # do with this failure. Citing it alongside 7.5s would send the reader to
    # the wrong setting — the same defect this message exists to fix.
    assert "DEFAULT_TIMEOUT_SECONDS" not in error


# ── branch statements: what `statement()` will and will not carry ───


@pytest.mark.parametrize(
    "source",
    [
        # The shape the finding was about: a write carrying its data inline,
        # where witan's guard would have scanned and redacted the same value
        # had it arrived as a named mutation's params.
        'insert Memory { content: "sk-live-abc123" }',
        # A branch statement the OTHER verb carries. `mutate` must not be a
        # way to smuggle in a read either.
        "branch list",
        # Near-misses, so the check is on the statement and not on a prefix.
        'branchcreate "x" from main',
        'create branch "x" from main',
        "",
    ],
)
def test_statement_refuses_anything_that_is_not_a_branch_mutation(
    monkeypatch, queries_dir, source
):
    """★ A SECURITY BOUNDARY, NOT A TYPO CHECK.

    `statement` takes GQ SOURCE, and witan's secret/PII write guard is
    `(query_name, params) -> params`: `scan.enforce.WriteGuard.__call__` looks
    the name up in `FIELD_MAP` and rewrites free-text params. It cannot scan
    raw source, and a statement hands it no params, so an arbitrary inline
    mutation on a guarded client would persist its literals unscanned. Witan
    builds every server client with that guard on by default.

    Restricting the source to branch statements is what makes that moot rather
    than merely unlikely: the only operand is a branch name.
    """
    client = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")

    with pytest.raises(ValueError, match="branch statements only"):
        client.statement("mutate", source)


def test_statement_refuses_a_verb_with_no_statement_form(monkeypatch, queries_dir):
    """`_http_execute` calls a write `verb == "mutate"` while
    `_WRITE_SUBCOMMANDS` also holds `load`/`optimize`/`cleanup`. Admitting only
    the two verbs that have a statement form is what keeps those in step."""
    client = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")

    with pytest.raises(ValueError, match="query or a mutate"):
        client.statement("load", "branch list")


@pytest.mark.parametrize(
    ("verb", "source"),
    [
        ("query", "branch list"),
        ("mutate", 'branch create "act-x/wip" from main'),
        ("mutate", 'branch delete "act-x/wip"'),
        ("mutate", 'branch merge "act-x/wip" into "main"'),
        ("mutate", '  branch  create  "act-x/wip" from main'),
    ],
)
def test_statement_carries_every_branch_form(
    _fake_http, monkeypatch, queries_dir, verb, source
):
    """The other side of the refusal: narrowing must not have cost a form the
    client actually uses, including the whitespace the callers do not emit but
    a hand-written one might."""
    FakeConnection.script = [ok({"rows": []})]
    client = _client(monkeypatch, "http://host:8080", queries_dir, graph_id="council")

    client.statement(verb, source)

    assert json.loads(FakeConnection.created[-1].requests[-1]["body"]) == {
        "query": source
    }


# ── branch statements: the params key that must not be sent ─────────


@pytest.mark.parametrize("verb", ["query", "mutate"])
def test_a_statement_body_omits_params_entirely(_fake_http, verb):
    """★ ``params=None`` MUST DROP THE KEY, not send an empty one.

    omnigraph 0.11 refuses a branch statement that carries ``params`` at all.
    Measured against the 0.11.0 server: ``{"query": "branch list", "params":
    {}}`` answers 400 ``a branch statement takes no name and no parameters``,
    while the same body without the key succeeds. An empty dict is the obvious
    thing to send and is exactly wrong, so it is pinned here rather than left
    to each caller to remember.
    """
    FakeConnection.script = [ok({"rows": []})]
    transport = ogh.PooledTransport("http://host:8080")

    getattr(transport, verb)("g", "branch list", None, None)

    body = json.loads(FakeConnection.created[-1].requests[-1]["body"])
    assert body == {"query": "branch list"}


@pytest.mark.parametrize("verb", ["query", "mutate"])
def test_a_named_call_still_sends_its_params_even_when_empty(_fake_http, verb):
    """The other side of the same rule: only a statement omits the key. A named
    query with nothing bound keeps sending ``{}``, which is what every caller
    but ``statement`` does."""
    FakeConnection.script = [ok({"rows": []})]
    transport = ogh.PooledTransport("http://host:8080")

    getattr(transport, verb)("g", "query q() { }", {}, None)

    body = json.loads(FakeConnection.created[-1].requests[-1]["body"])
    assert body == {"query": "query q() { }", "params": {}}


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            '{"commit": {"graph_commit_id": "c2", "parent_commit_id": "c1"}}',
            ("c2", "c1"),
        ),
        ('{"commit": {"graph_commit_id": "c2"}}', ("c2", None)),
        ('{"commit": null}', (None, None)),
        ("", (None, None)),
        ("not json", (None, None)),
    ],
)
def test_a_mutate_body_names_the_head_its_commit_replaced(body, expected):
    """Group commit refuses a queued write fenced on a replaced head without a
    round trip. If this stopped finding the parent, that refusal would quietly
    turn off (the server's 412 would still guard the write)."""
    assert og._commit_ids_from_mutate_body(body) == expected
