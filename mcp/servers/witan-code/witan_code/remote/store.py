"""Reaching a cluster code graph through the witan MCP tier (ADR-0005 path c).

The counterpart of :mod:`witan_code.ingest`, which serves these calls. Here the
indexer's ``OmnigraphClient`` is replaced by :class:`RemoteStoreClient`, whose
methods dispatch to the deployment's ``code_store_*`` tools instead of shelling
out to ``omnigraph --server``. Nothing above it changes: ``indexer.index_path``
and ``bridge.write_bindings`` keep every decision that needs a working tree,
and simply write through a different transport.

Only the operations the write path performs are mirrored. Store maintenance
(``optimize``/``cleanup``) and view reaping run against the storage root from
inside the cluster, not from whichever laptop finished indexing last, so they
refuse here with that as the message rather than silently doing nothing.

THE CONNECTION IS HELD OPEN. ``RemoteMCPProxy`` opens one per call, which is
right for a CLI read command and wrong here: an index is thousands of store
operations, and a TLS + MCP handshake each would dominate the run. So a
session owns one connection on a background event loop for as long as the
process indexes, and a dropped connection is reconnected once rather than
failing a run midway through its writes. The event loop lives on its own
thread because every caller in the write path is synchronous.
"""

from __future__ import annotations

import asyncio
import atexit
import threading
from typing import Any, Callable

from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from witan_core.observability import get_logger
from witan_core.remote.proxy import RemotePayloadTooLarge, payload_too_large

from witan_core import chunking

__all__ = [
    "RemotePayloadTooLarge",
    "RemoteStoreClient",
    "RemoteStoreUnsupported",
    "StoreSession",
    "close_sessions",
    "session_for",
]


logger = get_logger("witan.code.remote")


class RemoteStoreUnsupported(RuntimeError):
    """A store operation with no counterpart on the MCP tier."""


def _refuse_if_too_large(exc: BaseException, tool: str, url: str) -> None:
    """Re-raise a body refused for its size as a sentence. Otherwise no-op.

    Called on BOTH arms of ``StoreSession.call``'s handler, because a 413
    reaches this transport wearing either face: relayed through ToolHive's vMCP
    it is a ``ToolError``, while a direct connection raises an httpx status
    error that the generic arm would treat as a dropped socket. That second one
    is why this exists at all — the generic arm RECONNECTS AND RETRIES, so an
    oversized index batch would be sent, refused, sent again, refused again,
    and only then surface as a traceback. Retrying cannot help: the payload is
    the thing being rejected.

    ★ OPERATION-NEUTRAL, for the same reason the witan_core base message is.
    Every store call comes through here — reads, `code_store_views`, single
    mutations, the count-chunked `code_store_mutate_many`, and an `overwrite`
    load that is not chunked at all. An earlier revision asserted here that
    records were batched under 2 MiB and that an index had stopped part-way;
    that was false for all of those, and false for any `load` given a
    non-default ``max_bytes``. The operations that DO know their budget add it
    themselves below.
    """
    oversized = payload_too_large(exc)
    if oversized is None:
        return
    raise RemotePayloadTooLarge(
        f"{url} refused `{tool}`: the request body is over the deployment's "
        f"size cap ({oversized}). Retrying sends the same bytes to the same "
        "answer — the payload itself is what was rejected."
    ) from exc


def _load_refusal(exc: BaseException, *, batch: int, mode: str, max_bytes: int) -> str:
    """Add load-specific context to the neutral refusal above.

    Splits on ``mode`` because the two are not the same failure. A ``merge``
    load is byte-chunked, so the budget and the count already applied are both
    real and worth stating. An ``overwrite`` load is sent WHOLE — it cannot be
    chunked, since a partial overwrite would delete exactly what the later
    batches were going to restore — so quoting a batch budget at the reader
    would send them looking for a knob that does not apply.
    """
    if mode == "overwrite":
        return (
            f"{exc} An `overwrite` load is sent whole: it cannot be chunked, "
            "because a partial overwrite would delete what the remaining "
            "batches were going to restore. The record set itself has to get "
            "smaller, or the load has to run in-cluster instead."
        )
    size = chunking.describe_budget(max_bytes)
    applied = (
        f"The {batch} batch(es) before this one were applied — the index "
        "stopped part-way, it did not roll back."
        if batch
        else "Nothing was applied: this was the first batch."
    )
    return (
        f"{exc} This was batch {batch + 1} of a chunked load, sized against a "
        f"budget of {size} — so either one record is too large to split, or "
        f"the deployment's cap is below {size}. {applied}"
    )


# Added to the store tier after the first clients shipped, so its presence is
# asked rather than assumed — see `RemoteStoreClient.change_many`.
_MUTATE_MANY_TOOL = "code_store_mutate_many"


class _LoopThread:
    """A private event loop on a daemon thread, driven from synchronous code."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="witan-code-store", daemon=True
        )
        self._thread.start()

    def run(self, coro) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()


class StoreSession:
    """One authenticated MCP connection, reused across store operations.

    ``client_factory`` builds the MCP client from a freshly-minted token; tests
    substitute one that returns an in-memory client. The token is read per
    *connection* rather than per call — the connection outlives an access
    token's lifetime, so a long index reconnects (and re-reads the token)
    rather than presenting a stale one, which is what the reconnect below is
    for as much as a dropped socket is.
    """

    def __init__(
        self,
        url: str,
        token_provider: Callable[[], str],
        client_factory: Callable[[str], Client] | None = None,
    ) -> None:
        self.url = url
        self._token_provider = token_provider
        self._client_factory = client_factory or self._default_client
        self._loop: _LoopThread | None = None
        self._client: Client | None = None
        self._tools: frozenset[str] | None = None
        # One connection, and every caller in the write path is synchronous:
        # serialize whole calls rather than interleaving them on the session.
        self._lock = threading.Lock()

    def _default_client(self, token: str) -> Client:
        return Client(StreamableHttpTransport(self.url, auth=BearerAuth(token)))

    def call(self, tool: str, **arguments: Any) -> Any:
        """Invoke ``tool`` on the deployment and return its unwrapped result."""
        with self._lock:
            if self._client is None:
                self._connect()
            try:
                return self._invoke(tool, arguments)
            except ToolError as exc:
                # The server ran the tool and it failed — a refusal, a bad
                # query, a store error. Reconnecting would only run it again.
                _refuse_if_too_large(exc, tool, self.url)
                raise
            except Exception as exc:  # noqa: BLE001 — transport-shaped; retry once
                _refuse_if_too_large(exc, tool, self.url)
                # Info: one reconnect is routine (idle session dropped), but a
                # deployment that reconnects on every call is pathological and
                # invisible without this.
                logger.info("witan.code.remote.reconnecting", tool=tool, exc_info=True)
                self._disconnect()
                self._connect()
                return self._invoke(tool, arguments)

    def has_tool(self, tool: str) -> bool:
        """Whether the deployment serves ``tool``.

        For features the server gained after clients shipped: a client one
        release ahead of its deployment must fall back rather than fail, so ask
        instead of assuming. Cached per connection — the tool set is a property
        of the deployment, but a reconnect may land on a replica mid-rollout, so
        the cache dies with the connection rather than with the process.

        A server too old to answer at all is treated as not having the tool;
        the caller's fallback is what a refusal would have to end in anyway.
        THE FAILURE IS CACHED TOO, as an empty set — an index issues many
        batches, and re-asking a server that has already refused once would put
        a round trip and an exception on every one of them. Since the cache dies
        with the connection, a transport-level blip costs the slow path only
        until the next reconnect, not for the life of the process.
        """
        with self._lock:
            if self._client is None:
                self._connect()
            if self._tools is None:
                try:
                    self._tools = frozenset(t.name for t in self._list_tools())
                except Exception:  # noqa: BLE001 — fall back, don't fail
                    # Warning: an empty tool set makes every `has_tool` answer
                    # False, so the client silently takes its degraded path for
                    # a deployment that actually serves the tool.
                    logger.warning("witan.code.remote.list_tools_failed", exc_info=True)
                    self._tools = frozenset()
            return tool in self._tools

    def _list_tools(self) -> Any:
        assert self._loop is not None and self._client is not None
        return self._loop.run(self._client.list_tools())

    def _invoke(self, tool: str, arguments: dict) -> Any:
        assert self._loop is not None and self._client is not None
        result = self._loop.run(self._client.call_tool(tool, arguments))
        return result.data

    def _connect(self) -> None:
        if self._loop is None:
            self._loop = _LoopThread()
        client = self._client_factory(self._token_provider())
        self._loop.run(client.__aenter__())
        self._client = client

    def _disconnect(self) -> None:
        client, self._client = self._client, None
        self._tools = None
        if client is None or self._loop is None:
            return
        try:
            self._loop.run(client.__aexit__(None, None, None))
        except Exception:  # noqa: BLE001 — already broken; nothing to salvage
            # Debug only, and genuinely nothing to act on: this is teardown of a
            # connection already known to be dead, so a failure here changes
            # nothing a caller could respond to.
            logger.debug("witan.code.remote.disconnect_failed", exc_info=True)

    def close(self) -> None:
        with self._lock:
            self._disconnect()
            if self._loop is not None:
                self._loop.stop()
                self._loop = None


_sessions: dict[str, StoreSession] = {}
_sessions_lock = threading.Lock()


def session_for(url: str, token_provider: Callable[[], str]) -> StoreSession:
    """The process's session for ``url``, created on first use.

    Shared by every store a run touches — the per-repo graph and the bridge
    graph are two graphs on one deployment, and reconnecting between them
    would defeat holding the connection at all.
    """
    with _sessions_lock:
        if url not in _sessions:
            _sessions[url] = StoreSession(url, token_provider)
        return _sessions[url]


@atexit.register
def close_sessions() -> None:
    """Close every open session. Registered at exit; safe to call directly."""
    with _sessions_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        session.close()


class RemoteStoreClient:
    """An ``OmnigraphClient`` stand-in that writes through the MCP tier.

    ``graph`` names what the deployment should resolve — a canonical repo URI,
    or ``config.BRIDGE_GRAPH_ID`` — and ``branch`` the view, exactly as the
    subprocess client's ``--branch`` does. ``is_remote`` is True by
    construction: this transport exists only for shared cluster graphs, and
    the write guard (``graph.check_writable``) reads it to decide whether view
    ownership applies.
    """

    is_remote = True

    def __init__(
        self,
        graph: str,
        session: StoreSession,
        branch: str | None = None,
    ) -> None:
        self.graph = graph
        self.branch = branch
        self._session = session

    def __str__(self) -> str:
        return f"{self.graph} via {self._session.url}"

    # ── The write path's surface ──────────────────────────────────

    def read(self, query_file: str, query_name: str, params: dict) -> list[dict]:
        return self._session.call(
            "code_store_read",
            graph=self.graph,
            query=query_file,
            name=query_name,
            params=params,
            view=self.branch,
        )

    def change(
        self,
        query_file: str,
        query_name: str,
        params: dict,
        *,
        surface_conflict: bool = False,
    ) -> None:
        # `surface_conflict` is witan's CAS-claim knob; nothing in the code
        # graph's write path sets it, so it is accepted for signature parity
        # and deliberately not carried over the wire.
        self._session.call(
            "code_store_mutate",
            graph=self.graph,
            query=query_file,
            name=query_name,
            params=params,
            view=self.branch,
        )

    def change_many(
        self,
        steps: list[tuple[str, str, dict]],
        *,
        surface_conflict: bool = False,
        chunk_size: int | None = None,
    ) -> None:
        """Run several named mutations as one commit per chunk, like the subprocess client.

        The steps ride as params — file, name and params per step, the same
        triples ``change`` takes one at a time — and the server splices them
        into a single multi-statement mutation against its own ``queries_dir``.
        So a chunk costs one round trip and one Lance version, where it used to
        cost one of each PER STEP. A 200-file reindex's deletes are 400 steps:
        one call now, 400 before.

        ``chunk_size`` keeps the meaning it has on the subprocess client —
        statements per commit — and is applied here, one call per chunk, so
        commit granularity is identical across the two transports. It also
        bounds the wire payload, which is what the composed-argv cap bounded
        there. Same trade: chunks commit independently, so a failure part-way
        leaves the earlier ones applied.

        ``surface_conflict`` is not carried over the wire (see :meth:`change`),
        and a batch could not attribute a conflict to a step anyway.

        FEATURE-DETECTED, because this is a deployed contract: a client that
        has this code may be talking to a server that predates the batch tool.
        Such a server gets the old per-step loop, which is correct, just slow.
        """
        if not steps:
            return
        if chunk_size is not None and chunk_size < 1:
            raise ValueError(f"chunk_size must be >= 1, got {chunk_size}")
        if not self._session.has_tool(_MUTATE_MANY_TOOL):
            for query_file, query_name, params in steps:
                self.change(
                    query_file, query_name, params, surface_conflict=surface_conflict
                )
            return
        size = chunk_size or len(steps)
        for start in range(0, len(steps), size):
            try:
                self._session.call(
                    _MUTATE_MANY_TOOL,
                    graph=self.graph,
                    steps=[
                        {"query": query_file, "name": query_name, "params": params}
                        for query_file, query_name, params in steps[
                            start : start + size
                        ]
                    ],
                    view=self.branch,
                )
            except RemotePayloadTooLarge as exc:
                # ★ THIS CHUNKING IS BY STATEMENT COUNT, NOT BYTES — so unlike
                # `load` there is no byte budget to quote, and pointing the
                # reader at one would send them to a knob that does not exist
                # here. `chunk_size` is the knob, and it is the caller's.
                raise RemotePayloadTooLarge(
                    f"{exc} This batch carried {min(size, len(steps) - start)} "
                    "statements. `change_many` chunks by statement COUNT, not "
                    "by bytes, so there is no byte budget bounding it — pass a "
                    f"smaller `chunk_size` (currently {size}). "
                    + (
                        f"The {start} statement(s) before it were applied — "
                        "chunks commit independently, so this stopped part-way."
                        if start
                        else "Nothing was applied: this was the first chunk."
                    )
                ) from exc

    def load(
        self,
        records: list[dict],
        mode: str = "merge",
        *,
        max_bytes: int = chunking.MCP_LOAD_MAX_BYTES,
    ) -> None:
        """Bulk-load records through the MCP tier's ``code_store_load``.

        Chunked on the same terms as :meth:`graph.OmnigraphClient.load`, for the
        same reason one layer down: ``code_store_load`` hands the records to an
        ``omnigraph load`` on the server side, so a repo-scale call runs into
        the same buffered-body ceiling. This transport adds one of its own — the
        records ride as a JSON tool parameter over the MCP session — which the
        byte budget bounds as well.

        ★ That second ceiling is the tighter one and the default reflects it:
        ``MCP_LOAD_MAX_BYTES``, not ``LOAD_MAX_BYTES``. The MCP Python SDK
        rejects request bodies over 4 MiB before parsing them. This defaulted to
        the omnigraph budget (8 MiB) until 2026-08-07, which made a large enough
        index fail here exactly as ``migrate merge`` did against the deployment
        — ``413 Request body too large``, from the SDK rather than from
        omnigraph.

        ``overwrite`` is not chunked here either; see the note there.
        """
        if not records:
            return
        batches = (
            [records]
            if mode == "overwrite"
            else chunking.chunk_records(records, max_bytes)
        )
        for index, batch in enumerate(batches):
            try:
                self._session.call(
                    "code_store_load",
                    graph=self.graph,
                    records=batch,
                    mode=mode,
                    view=self.branch,
                )
            except RemotePayloadTooLarge as exc:
                raise RemotePayloadTooLarge(
                    _load_refusal(exc, batch=index, mode=mode, max_bytes=max_bytes)
                ) from exc

    def ensure_branch(self) -> None:
        if self.branch is None:
            return
        self._session.call("code_store_open", graph=self.graph, view=self.branch)

    def list_branches(self) -> list[str]:
        return self._session.call("code_store_views", graph=self.graph)

    # ── Refusals ──────────────────────────────────────────────────

    def _cluster_only(self, what: str) -> RemoteStoreUnsupported:
        return RemoteStoreUnsupported(
            f"{what} runs inside the cluster, against the storage root — not "
            f"from a client of {self._session.url}."
        )

    def delete_branch(self, name: str) -> None:
        raise self._cluster_only("Reaping stale branch views")

    def branch_last_write(self, name: str) -> float | None:
        raise self._cluster_only("Reading a view's last-write time")

    def optimize(self) -> str:
        raise self._cluster_only("Compaction")

    def cleanup(self, *, keep: int | None = None, older_than: str | None = None) -> str:
        raise self._cluster_only("Version cleanup")
