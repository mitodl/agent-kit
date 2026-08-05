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
import json
import os
import shutil
import socket
import threading

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
    def __init__(self, status: int, body: str = "", will_close: bool = False):
        self.status = status
        self.will_close = will_close
        self._body = body.encode()

    def read(self) -> bytes:
        return self._body


class FakeConnection:
    """One scripted connection. ``script`` is consumed one entry per request.

    An entry is either a :class:`FakeResponse` or an exception instance, which
    is raised from ``request()`` — i.e. after the connection was established,
    which is the mid-flight case the policy must treat differently from a
    connect failure.
    """

    created: list[FakeConnection] = []

    connect_error: BaseException | None = None
    script: list = []

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
        (500, "manifest table version mismatch", ogh.RETRYABLE),
        (500, "head is ahead of manifest, run omnigraph repair", ogh.NEEDS_REPAIR),
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
    """`_execute` repairs before it sleeps, so the classifier must agree.

    A message naming both conditions has to resolve to the repair, or the client
    retries a store that cannot succeed until it is reconciled.
    """
    message = "stale view: head is ahead of manifest, run omnigraph repair"
    assert ogh.classify_status(500, message) == ogh.NEEDS_REPAIR


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


def test_midflight_failure_on_a_fresh_connection_is_unavailable(_fake_http):
    """Nothing was sent — `connect()` succeeded and `request()` failed on a
    connection this call opened, so there is no committed-write ambiguity."""
    _fake_http.script = [ConnectionResetError("reset")]
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


def test_a_client_injecting_extra_cli_args_stays_on_the_subprocess(
    monkeypatch, queries_dir
):
    """THE SAFETY PROPERTY. witan-code's branched clients add `--branch <view>`.

    omnigraph 0.8.1's HTTP API documents no request-side branch selector, so
    routing such a call over HTTP would execute it against `main` — a WIP
    reindex landing in the shared graph, which is the exact thing branch views
    exist to prevent. The guard is on `_extra_args`, not on a `branch`
    attribute, so a future subclass arg is caught by the same check instead of
    being silently dropped.
    """

    class BranchedClient(OmnigraphClient):
        def _extra_args(self, subcommand):
            return ["--branch", "act-alice/wip"]

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    client = BranchedClient("http://host:8080", queries_dir, graph_id="council")

    assert client._http_transport() is None


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
