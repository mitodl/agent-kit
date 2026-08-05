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

__all__ = [
    "RemoteStoreClient",
    "RemoteStoreUnsupported",
    "StoreSession",
    "close_sessions",
    "session_for",
]


class RemoteStoreUnsupported(RuntimeError):
    """A store operation with no counterpart on the MCP tier."""


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
            except ToolError:
                # The server ran the tool and it failed — a refusal, a bad
                # query, a store error. Reconnecting would only run it again.
                raise
            except Exception:  # noqa: BLE001 — transport-shaped; retry once
                self._disconnect()
                self._connect()
                return self._invoke(tool, arguments)

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
        if client is None or self._loop is None:
            return
        try:
            self._loop.run(client.__aexit__(None, None, None))
        except Exception:  # noqa: BLE001 — already broken; nothing to salvage
            pass

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
        """Signature parity with the subprocess client — but NOT batched.

        The subprocess client collapses steps into one multi-statement ``mutate``
        and therefore one Lance version. This transport cannot: ``code_store_mutate``
        takes a query FILE plus a name and resolves it server-side, so there is no
        way to hand it a composed body. Each step stays its own call, and its own
        commit, exactly as before.

        That makes this the one remaining per-row writer. Closing it needs a
        batch endpoint on the MCP tier — tracked in
        ``tk-batch-endpoint-for-the-witan-code-mcp-write-tier-22f089``.

        ``chunk_size`` is accepted and ignored: there is no composed argv to cap
        when every step is already its own request.
        """
        for query_file, query_name, params in steps:
            self.change(
                query_file, query_name, params, surface_conflict=surface_conflict
            )

    def load(self, records: list[dict], mode: str = "merge") -> None:
        if not records:
            return
        self._session.call(
            "code_store_load",
            graph=self.graph,
            records=records,
            mode=mode,
            view=self.branch,
        )

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
