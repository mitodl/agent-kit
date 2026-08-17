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

#: A conditional write whose stated precondition was false — omnigraph's
#: ``/mutate/if-graph-commit`` answering 412, or the CLI's ``--if-commit``
#: exiting 4 (upstream #470).
#:
#: ★ TERMINAL, AND DELIBERATELY NOT :data:`RETRYABLE`, WHICH IS THE WHOLE POINT
#: OF GIVING IT ITS OWN NAME. 409 and 412 look like the same family and mean
#: opposite things:
#:
#:     409  the branch head moved under you. Nothing was written. Try again —
#:          the same write is still what you want.
#:     412  the precondition YOU STATED is false. Nothing was written. Do NOT
#:          re-send this write; re-read and decide what you now want.
#:
#: Retrying a 412 re-applies a claim over whoever won the race, which is the
#: exact failure ``surface_conflict`` exists to prevent. Upstream is explicit
#: that it "is never replayed by the insert-only reprepare loop", and this
#: classification is what makes that true on our side too.
PRECONDITION_FAILED = "precondition_failed"

#: The branch-wide write barrier: "recovery required for operation …: pending
#: Load recovery operation blocks writes on branch 'main'".
#:
#: Transient and self-clearing — reproduced locally in ~20s with six concurrent
#: single-row appends to DISTINCT keys, where five losers got this and a plain
#: write issued immediately afterwards succeeded with no ``omnigraph repair``
#: and no intervention. So it is the store saying "come back", the same species
#: as the admission cap, and it gets its own budget for the same reason.
#:
#: ★ NOT :data:`NEEDS_REPAIR`. Running ``omnigraph repair --force`` on a store
#: that merely had a concurrent writer is a far heavier hammer than the
#: situation warrants, and the repro showed nothing needed repairing.
#:
#: ★ AND NOT SOMETHING ``surface_conflict`` MAY SWALLOW. A CAS caller must lose
#: a GENUINE race rather than treat a bystander barrier as a lost race — this
#: is a different condition from the conflict that primitive exists for, and
#: collapsing them would make a claim report ``lost_race`` to a caller that
#: never actually contended with anyone.
RECOVERY_REQUIRED = "recovery_required"

#: The conditional-write precondition header (upstream #470). Sent raw and
#: exactly once, and ONLY to the dedicated routes below — the ordinary
#: ``/mutate`` rejects it outright rather than ignoring an unknown header,
#: which is what stops a precondition from being silently dropped.
IF_GRAPH_COMMIT_HEADER = "Omnigraph-If-Graph-Commit"

#: Path suffix selecting the conditional variant of a write route.
IF_GRAPH_COMMIT_SUFFIX = "/if-graph-commit"

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

    ``retry_after`` is the server's own ``Retry-After``, in seconds, when it sent
    one. The CLI path cannot have it — omnigraph's error path prints the JSON
    body's message and discards the response headers — so this is a signal only
    the HTTP transport can carry, and the policy loop treats its absence as
    "no hint" rather than as "retry immediately".
    """

    kind: str
    body: str = ""
    error: str = ""
    status: int | None = None
    retry_after: float | None = None


def parse_retry_after(value: str | None) -> float | None:
    """Seconds from a ``Retry-After`` header value, or None if there is no usable one.

    Only the delta-seconds form is read. RFC 9110 also allows an HTTP-date, but
    omnigraph-server sends delta-seconds (``Retry-After: 60``), and honouring a
    date would mean trusting the caller's clock against the server's — a wrong
    answer in the one direction that matters, since a skewed clock turns "wait a
    minute" into "wait an hour" or into no wait at all. Anything unparseable is
    None, which the policy loop reads as "the server gave no hint" and falls
    back to its own schedule.

    Negative values are dropped for the same reason a missing header is: a
    sleep of a negative duration is not what the server asked for, and treating
    it as zero would silently turn a malformed header into a hot retry.
    """
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except (ValueError, AttributeError):
        return None
    return seconds if seconds >= 0 else None


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
    lowered = message.lower()
    if status == 412 or "precondition_failure" in lowered:
        # See PRECONDITION_FAILED: terminal, and the inverse of the 409 rule
        # further down. Checked BEFORE the message markers because a
        # precondition failure is not up for reinterpretation by prose — the
        # server has told us our stated condition was false, and no wording
        # makes that retryable.
        return PRECONDITION_FAILED
    if status == 503:
        # ★ NOT EVERY 503 IS "WAIT FOR THE SERVER". The blanket rule below rests
        # on a premise this one breaks: that a 503 proves the request was
        # rejected rather than applied. `recovery_required` carries the
        # opposite warning — upstream #470 says "table effects may already
        # require recovery" — so retrying it for the full unavailable budget
        # could re-apply a write whose effects are partly present. That is the
        # indeterminate write this project spent four defects removing,
        # arriving through the status code we trust most.
        #
        # It gets its own kind and its own budget instead: transient and
        # self-clearing (see RECOVERY_REQUIRED), but not a plain "server is
        # down", and never surfaced to a CAS caller as a lost race.
        if "recovery required" in lowered or "recovery_required" in lowered:
            return RECOVERY_REQUIRED
        # The server is up enough to answer but not to serve. Same remedy as an
        # unreachable server (wait for it), and the response proves the request
        # was rejected rather than applied, so it is safe for writes too.
        return UNAVAILABLE
    if any(marker in lowered for marker in ("ahead of manifest", "omnigraph repair")):
        return NEEDS_REPAIR
    if any(
        marker in lowered
        for marker in ("stale view", "manifest table version", "refresh and retry")
    ):
        return RETRYABLE
    if status == 409:
        # ★ A CONFLICT IS THE ONE STATUS THAT MEANS "RETRY ME" MOST LITERALLY,
        # and it was falling through to FATAL because this function only ever
        # recognised a conflict by its PROSE. The markers above were tuned
        # against the wordings omnigraph used when they were written; 0.10.0
        # rejects a racing writer with a sentence none of them match:
        #
        #   write authority 'graph_head:main' changed during preparation
        #   (expected 01M08E24Y…, current 01M08E27K…) — reprepare from the
        #   current branch state (HTTP 409, conflict)
        #
        # Classifying on the STATUS fixes that permanently: the server can
        # reword the precondition — and it has, twice — without silently
        # turning a losable race back into a hard failure. Message markers stay
        # first so a 409 that also names a repair condition still repairs.
        #
        # SAFE FOR WRITES, which is why it may sit alongside 429/503 rather
        # than only being retried for idempotent calls: the server rejected
        # this at PREPARE, before the commit, so the response proves nothing
        # was written. That is the same guarantee that makes 503 safe here.
        #
        # ★ AND IT IS WHAT MAKES A COMPARE-AND-SWAP LOSER LEGIBLE. `_retry_loop`
        # turns RETRYABLE into `OmnigraphConflict` for a `surface_conflict`
        # caller, which `task_claim` catches to re-read and return a structured
        # `{"claimed": false, "reason": "lost_race"}`. Left FATAL, that whole
        # path is unreachable and 6 of 8 racers got an opaque RuntimeError
        # instead of an answer they could act on.
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
        if_graph_commit: str | None = None,
    ) -> Outcome:
        """POST ``payload`` as JSON to ``path`` and classify the result."""
        return self._send(
            "POST",
            path,
            json.dumps(payload),
            token,
            idempotent,
            if_graph_commit=if_graph_commit,
        )

    def get(self, path: str, token: str | None) -> Outcome:
        """GET ``path``. A read, so always safe to repeat."""
        return self._send("GET", path, None, token, True)  # noqa: FBT003

    def _send(
        self,
        method: str,
        path: str,
        body: str | None,
        token: str | None,
        idempotent: bool,  # noqa: FBT001 — positional from two private callers
        *,
        if_graph_commit: str | None = None,
    ) -> Outcome:
        """Send one request and classify the result.

        ``idempotent`` says whether re-sending is safe, and it is what decides
        every ambiguous case:

        - A failure during ``connect()`` is pre-send no matter what the caller
          is doing, so it is ``UNAVAILABLE`` (retryable) for reads and writes
          alike. This is the only failure that can be classified without
          consulting ``idempotent``.
        - A failure once the request is in flight — ``request()``,
          ``getresponse()`` or ``read()`` — is ``UNAVAILABLE`` only when
          idempotent, and ``FATAL`` otherwise. Being the connection's opener
          proves nothing here: ``connect()`` returning means the TCP handshake
          completed, not that the request was never written, and by
          ``getresponse()`` it definitely was.
        - A *reused* connection dying before any response byte additionally
          gets one immediate retry on a fresh connection when idempotent. That
          is the stale-keep-alive case, and it is a fast path, not a different
          safety rule.
        """
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body.encode()))
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if if_graph_commit is not None:
            # Upstream #470 requires EXACTLY ONE of these, sent raw, and only to
            # the dedicated `/…/if-graph-commit` routes — the ordinary routes
            # reject the header rather than ignoring it. That fail-closed design
            # is deliberate and worth preserving on this side: an old server
            # 404s the conditional route before executing anything, instead of
            # silently dropping the precondition and mutating unconditionally.
            headers[IF_GRAPH_COMMIT_HEADER] = if_graph_commit

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
                conn.request(method, path, body=body, headers=headers)
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
                # MID-FLIGHT. Everything in the `try` above is past the point of
                # no return: `request()` writes to the socket, `getresponse()`
                # means it was written, and `read()` means the server already
                # answered. So the write may have committed, and `UNAVAILABLE`
                # would hand it to a retry loop that re-sends it.
                #
                # Whether THIS call opened the connection is irrelevant here —
                # `connect()` succeeding says the handshake completed, not that
                # nothing was sent. Keying on that (rather than on `idempotent`)
                # was a real bug: it let a fresh-connection mutate be silently
                # re-applied, which is the exact thing `surface_conflict` exists
                # to prevent, one layer lower where no caller can opt out.
                return Outcome(
                    kind=UNAVAILABLE if idempotent else FATAL,
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
                retry_after=parse_retry_after(response.getheader("Retry-After")),
            )

        # Unreachable: the loop either returns or continues exactly once.
        raise AssertionError("_send() retry loop fell through")

    def query(self, graph_id: str, source: str, params: dict, token: str | None):
        """``POST /graphs/<id>/query`` — a read. Always safe to repeat."""
        return self.post(
            f"/graphs/{graph_id}/query",
            {"query": source, "params": params},
            token,
            idempotent=True,
        )

    def mutate(
        self,
        graph_id: str,
        source: str,
        params: dict,
        token: str | None,
        if_graph_commit: str | None = None,
    ):
        """``POST /graphs/<id>/mutate`` — a write. Never repeated implicitly.

        With ``if_graph_commit`` this becomes ``…/mutate/if-graph-commit``,
        applying only while that branch head is still current. A stale token is
        a terminal 412 (:data:`PRECONDITION_FAILED`), never a silent
        unconditional write — the route and the header move together precisely
        so there is no way to ask for a precondition and not get one.
        """
        path = f"/graphs/{graph_id}/mutate"
        if if_graph_commit is not None:
            path += IF_GRAPH_COMMIT_SUFFIX
        return self.post(
            path,
            {"query": source, "params": params},
            token,
            idempotent=False,
            if_graph_commit=if_graph_commit,
        )

    def graphs(self, token: str | None):
        """``GET /graphs`` — the server's graph registry.

        The one server-scoped question witan asks. It exists here rather than as
        a caller's own socket because the CLI cannot answer it at all: on 0.8.1
        ``omnigraph graphs list --server <url>`` fetches the listing and then
        refuses to print it against any multi-graph server, demanding a
        ``--graph <id>`` that makes no sense for a listing. The data was never
        the problem — the error text carries the full list — so the fix is to
        ask the server directly rather than to wait for the CLI.
        """
        return self.get("/graphs", token)
