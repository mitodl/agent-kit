"""Pooled HTTP transport for a deployed omnigraph-server.

``OmnigraphClient`` drives the ``omnigraph`` CLI as a subprocess for every call.
Locally a subprocess has to run anyway — it is the thing that opens the Lance
directory. Against a remote ``omnigraph-server`` it is pure overhead on top of an
HTTP roundtrip, and the spike measured it as **77-81% of every read** on a
compacted store (25.9ms via the CLI vs 5.1ms over a keep-alive connection at one
worker; 45.8 vs 10.7 at eight). The bare fork/exec floor is only 6.7-14.7ms, so
the subprocess costs 2-3x the raw spawn — the rest is per-invocation CLI work
(config resolution, ``.gq`` parse, client construction, a fresh TCP connection
every time). See docs/design/omnigraph-remote-call-overhead-spike.md.

This module is that connection. It is deliberately NOT a second
``OmnigraphClient``: omnigraph-server 0.8.1 exposes only ``query``, ``mutate``
and ``graphs`` over HTTP, while the client's surface also includes ``load``,
``branch``, ``commit``, ``optimize``, ``cleanup``, ``schema apply`` and
``repair``, none of which have an HTTP form. A second client class would have to
either drop those (breaking witan-code's branch views and both servers'
maintenance) or shell out for them anyway. So this is a transport for the two
verbs that have an HTTP equivalent, and everything else stays on the CLI.

WHY IT RETURNS AN OUTCOME RATHER THAN RAISING

``OmnigraphClient._execute`` already carries a carefully-tuned policy loop:
connect-failure retries budgeted against a measured server restart, per-actor
admission-cap backoff, optimistic-concurrency retry/repair, and
``surface_conflict`` for CAS callers. That policy must NOT be duplicated here —
two copies would drift, and the drift would show up as the two transports
behaving differently under load, which is close to undebuggable.

Instead every call returns an :class:`Outcome` carrying an explicit ``kind``,
and ``_execute`` runs the same loop over it that it runs over a subprocess
result. The subprocess path keeps classifying from stderr text exactly as
before; this path classifies from HTTP status and exception type, which is
strictly better information. One policy, two ways of reaching it.

STDLIB ONLY — ``witan_core``'s base modules take no dependencies (see the
package's pyproject), and ``http.client`` is also precisely what the spike
measured, so these numbers describe this code rather than a different client.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import threading
import time
import urllib.parse
from typing import NamedTuple

# Classification vocabulary shared with the subprocess path. `_execute` maps a
# subprocess's stderr onto these same names, so a condition is handled
# identically no matter which transport reported it.
UNAVAILABLE = "unavailable"
ADMISSION_CAP = "admission_cap"
RETRYABLE = "retryable"
NEEDS_REPAIR = "needs_repair"
FATAL = "fatal"
OK = "ok"

# A pooled connection idle longer than this is closed and reopened rather than
# reused. It is not a performance knob — it is what keeps the connect-failure
# classification honest.
#
# A server that closed an idle keep-alive connection fails the NEXT request at
# send time, and at that point we cannot prove whether the request reached it.
# For a read that ambiguity is harmless (retry); for a mutate it is not (the
# write may have committed, and re-applying it is exactly what `surface_conflict`
# exists to prevent). Reopening after a short idle makes almost every such
# failure happen during `connect()` instead — where it is provably pre-send and
# can be retried safely for reads and writes alike.
#
# Deliberately well below any plausible server idle timeout: the cost of being
# wrong in this direction is one extra TCP handshake on a cold call, against a
# spurious hard error on a write in the other.
IDLE_REUSE_MAX_SECONDS = 5.0

# No timeout at all is what the subprocess path had (`subprocess.run` with no
# `timeout=`), so anything finite is a tightening. Set well above the worst
# measured write — p50 hit 1.65s at 8 concurrent writers, and that was a
# server-side serialization limit that grows with load — so this only fires on a
# genuinely wedged server rather than on a slow-but-working one.
DEFAULT_TIMEOUT_SECONDS = 120.0


class Outcome(NamedTuple):
    """The result of one HTTP attempt, in the shape ``_execute``'s loop wants.

    ``kind`` is one of the module-level classification constants. ``body`` is the
    response body verbatim — the same JSON text the CLI would have printed on
    stdout, so callers parse one format regardless of transport, with no
    re-serialization on the hot path. ``error`` is the human-readable message for
    a failure, and is what ends up in the ``RuntimeError`` the client raises.
    """

    kind: str
    body: str = ""
    error: str = ""
    status: int | None = None


def classify_status(status: int, message: str) -> str:
    """Map an HTTP status + error message onto the shared classification names.

    The status is the primary signal and is strictly better than what the CLI
    path had: a 429 is unambiguously the per-actor admission cap, where the CLI
    could only match the message text. The message is still consulted for the
    conflict/repair conditions, which the server reports as a 4xx/5xx with the
    same wording the CLI surfaced — so the markers those were tuned against
    remain the right discriminator.
    """
    if status == 429:
        return ADMISSION_CAP
    if status == 503:
        # The server is up enough to answer but not to serve. Same remedy as an
        # unreachable server (wait for it), and the response proves the request
        # was rejected rather than applied, so it is safe for writes too.
        return UNAVAILABLE
    lowered = message.lower()
    if any(marker in lowered for marker in ("ahead of manifest", "omnigraph repair")):
        return NEEDS_REPAIR
    if any(
        marker in lowered
        for marker in ("stale view", "manifest table version", "refresh and retry")
    ):
        return RETRYABLE
    return FATAL


def error_message(status: int, body: str) -> str:
    """Pull the server's own message out of an error response.

    omnigraph-server answers with ``{"error": …, "code": …}``; anything else
    (an HTML 502 from an ingress, an empty body) is passed through verbatim
    rather than swallowed, because a proxy failing in front of the server is a
    real and confusing case and hiding its response would make it unreadable.
    """
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        detail = parsed.get("error") or parsed.get("message")
        if isinstance(detail, str) and detail:
            code = parsed.get("code")
            return f"{detail} (HTTP {status}{f', {code}' if code else ''})"
    stripped = (body or "").strip()
    return f"HTTP {status}{f': {stripped}' if stripped else ''}"


class PooledTransport:
    """Keep-alive connections to one omnigraph-server, one per calling thread.

    Both MCP servers dispatch tool calls on a thread pool, so the pool is keyed
    by thread rather than guarded by a mutex: ``http.client`` connections are not
    thread-safe, and sharing one behind a lock would serialize precisely the
    concurrent reads the spike showed the server handles well (p50 degrades only
    5.1 → 10.7ms from 1 to 8 workers while throughput rises ~4x).

    A per-thread connection is also why no cleanup is needed: threads in a pool
    are long-lived, and a connection dies with its thread.
    """

    def __init__(self, server_url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        parts = urllib.parse.urlsplit(server_url)
        if parts.scheme not in ("http", "https"):
            raise ValueError(
                f"pooled transport needs an http(s) server url, got {server_url!r}"
            )
        self.server_url = server_url
        self._scheme = parts.scheme
        self._host = parts.hostname or ""
        self._port = parts.port
        self._timeout = timeout
        self._local = threading.local()

    # ── connection management ─────────────────────────────────────

    def _new_connection(self) -> http.client.HTTPConnection:
        if self._scheme == "https":
            return http.client.HTTPSConnection(
                self._host,
                self._port,
                timeout=self._timeout,
                context=ssl.create_default_context(),
            )
        return http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)

    def _checkout(self) -> tuple[http.client.HTTPConnection, bool]:
        """This thread's connection and whether it is being reused.

        The reuse flag is what lets the caller tell a provably-pre-send failure
        from an ambiguous one — see :data:`IDLE_REUSE_MAX_SECONDS`.
        """
        conn = getattr(self._local, "conn", None)
        last_used = getattr(self._local, "last_used", 0.0)
        if conn is not None and time.monotonic() - last_used > IDLE_REUSE_MAX_SECONDS:
            self._discard()
            conn = None
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
            return conn, False
        return conn, True

    def _discard(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except OSError:
                # Closing a socket that is already gone is not a failure worth
                # propagating — we are discarding it either way.
                pass
        self._local.conn = None

    # ── the call ──────────────────────────────────────────────────

    def post(
        self,
        path: str,
        payload: dict,
        token: str | None,
        *,
        idempotent: bool,
    ) -> Outcome:
        """POST ``payload`` as JSON to ``path`` and classify the result.

        ``idempotent`` says whether re-sending is safe, and controls exactly one
        behaviour: a *reused* connection that dies before any response byte
        arrives is retried once on a fresh connection when idempotent, and
        surfaced when not. That is the stale-keep-alive case, where the request
        may or may not have reached the server; for a read the ambiguity does not
        matter, and for a mutate it very much does.
        """
        body = json.dumps(payload)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Content-Length": str(len(body.encode())),
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        for attempt in range(2):
            conn, reused = self._checkout()
            try:
                if not reused:
                    # Connect explicitly so an unreachable server fails HERE,
                    # where nothing can have been sent. That is the whole basis
                    # for retrying a connect failure on a write — the same
                    # reasoning the subprocess path applies to the CLI's
                    # "tcp connect error".
                    conn.connect()
            except (socket.gaierror, OSError) as exc:
                self._discard()
                return Outcome(
                    kind=UNAVAILABLE,
                    error=f"could not connect to {self.server_url}: {exc}",
                )

            try:
                conn.request("POST", path, body=body, headers=headers)
                response = conn.getresponse()
                raw = response.read()
            except (http.client.HTTPException, OSError) as exc:
                self._discard()
                if reused and idempotent and attempt == 0:
                    # Stale keep-alive: the server closed an idle connection and
                    # we found out by trying to use it. Safe to repeat because
                    # the caller said so; a fresh connection is what the retry
                    # gets, since _discard cleared the pooled one.
                    continue
                return Outcome(
                    kind=UNAVAILABLE if not reused else FATAL,
                    error=f"request to {self.server_url}{path} failed: {exc}",
                )

            self._local.last_used = time.monotonic()
            if response.will_close:
                self._discard()

            text = raw.decode("utf-8", errors="replace")
            if 200 <= response.status < 300:
                return Outcome(kind=OK, body=text, status=response.status)
            message = error_message(response.status, text)
            return Outcome(
                kind=classify_status(response.status, message),
                error=message,
                status=response.status,
            )

        # Unreachable: the loop either returns or continues exactly once.
        raise AssertionError("post() retry loop fell through")

    def query(self, graph_id: str, source: str, params: dict, token: str | None):
        """``POST /graphs/<id>/query`` — a read. Always safe to repeat."""
        return self.post(
            f"/graphs/{graph_id}/query",
            {"query": source, "params": params},
            token,
            idempotent=True,
        )

    def mutate(self, graph_id: str, source: str, params: dict, token: str | None):
        """``POST /graphs/<id>/mutate`` — a write. Never repeated implicitly."""
        return self.post(
            f"/graphs/{graph_id}/mutate",
            {"query": source, "params": params},
            token,
            idempotent=False,
        )
