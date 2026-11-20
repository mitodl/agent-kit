"""An in-process harness for language-server tests.

A real pygls client talks JSON-RPC to :func:`witan_core.lsp.server.build_server`
over two OS pipes, with the server's stdio loop on a thread of the test
process. Nothing is spawned and nothing listens on a socket, so a test sees the
same framing, capability negotiation and notification flow an editor does,
inside whatever environment the suite's hermetic conftest set up.

Used by witan-core's, witan-council's and witan-code's suites; it needs the
``lsp`` extra, which each of their ``test`` dependency groups installs.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import AsyncIterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lsprotocol import types
from pygls.io_ import StdinAsyncReader, StdoutWriter, run_async
from pygls.lsp.client import LanguageClient

from .host import STATUS_COMMAND, Host
from .server import build_server

__all__ = ["Session", "lsp_session", "spawned_status"]

TIMEOUT_SECONDS = 10.0
STARTUP_TIMEOUT_SECONDS = 120.0
"""A spawned CLI imports its whole package before it answers ``initialize``,
which took longer than ``TIMEOUT_SECONDS`` with the five suites running in
parallel (``just test-all``)."""


@dataclass
class Session:
    client: LanguageClient
    initialize_result: types.InitializeResult
    diagnostics: dict[str, list[types.Diagnostic]] = field(default_factory=dict)
    log_messages: list[str] = field(default_factory=list)
    _published: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def capabilities(self) -> types.ServerCapabilities:
        return self.initialize_result.capabilities

    async def status(self) -> dict[str, Any]:
        """The server's ``witan.status`` command result."""
        return await self.command(STATUS_COMMAND)

    async def command(self, command: str, *arguments: Any) -> Any:
        return await asyncio.wait_for(
            self.client.workspace_execute_command_async(
                types.ExecuteCommandParams(command=command, arguments=list(arguments))
            ),
            TIMEOUT_SECONDS,
        )

    async def open(self, path: Path, text: str = "") -> list[types.Diagnostic]:
        """Open ``path`` and return the diagnostics the server publishes for it."""
        uri = path.as_uri()
        self.diagnostics.pop(uri, None)
        self._published.clear()
        self.client.text_document_did_open(
            types.DidOpenTextDocumentParams(
                text_document=types.TextDocumentItem(
                    uri=uri, language_id="python", version=1, text=text
                )
            )
        )
        async with asyncio.timeout(TIMEOUT_SECONDS):
            while uri not in self.diagnostics:
                await self._published.wait()
                self._published.clear()
        return self.diagnostics[uri]


@asynccontextmanager
async def lsp_session(
    host: Host,
    folders: Sequence[Path],
    *,
    target: str | None = None,
    client_capabilities: types.ClientCapabilities | None = None,
) -> AsyncIterator[Session]:
    """Start a server for ``host``, initialize it with ``folders``, and yield
    a :class:`Session`. Shuts the server down and joins its thread on exit.

    ``client_capabilities`` defaults to none at all, which is a client with no
    CodeLens, so the host's lens fallback is on.
    """
    server = build_server(host, target=target)
    to_server_r, to_server_w = os.pipe()
    to_client_r, to_client_w = os.pipe()
    server_thread = threading.Thread(
        target=server.start_io,
        args=(os.fdopen(to_server_r, "rb"), os.fdopen(to_client_w, "wb")),
        name="witan-lsp-test-server",
        daemon=True,
    )
    server_thread.start()

    client = LanguageClient("witan-lsp-test", "0")
    diagnostics: dict[str, list[types.Diagnostic]] = {}
    log_messages: list[str] = []
    published = asyncio.Event()

    @client.feature(types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)
    def on_diagnostics(params: types.PublishDiagnosticsParams) -> None:
        diagnostics[params.uri] = list(params.diagnostics)
        published.set()

    @client.feature(types.WINDOW_LOG_MESSAGE)
    def on_log(params: types.LogMessageParams) -> None:
        log_messages.append(params.message)
        published.set()

    to_server = os.fdopen(to_server_w, "wb")
    client.protocol.set_writer(StdoutWriter(to_server))
    reader_pool = ThreadPoolExecutor(max_workers=1)
    reader = asyncio.create_task(
        run_async(
            stop_event=client._stop_event,
            reader=StdinAsyncReader(os.fdopen(to_client_r, "rb"), reader_pool),
            protocol=client.protocol,
        )
    )
    try:
        result = await asyncio.wait_for(
            client.initialize_async(
                types.InitializeParams(
                    capabilities=client_capabilities or types.ClientCapabilities(),
                    workspace_folders=[
                        types.WorkspaceFolder(uri=p.as_uri(), name=p.name)
                        for p in folders
                    ],
                )
            ),
            TIMEOUT_SECONDS,
        )
        client.initialized(types.InitializedParams())
        session = Session(client, result, diagnostics, log_messages, published)
        # The server resolves folders when `initialized` arrives and reports
        # them in one log message; waiting for it means no test races that.
        async with asyncio.timeout(TIMEOUT_SECONDS):
            while not log_messages:
                await published.wait()
                published.clear()
        yield session
    finally:
        await asyncio.wait_for(client.shutdown_async(None), TIMEOUT_SECONDS)
        client.exit(None)
        to_server.close()
        await asyncio.to_thread(server_thread.join, TIMEOUT_SECONDS)
        await asyncio.wait_for(reader, TIMEOUT_SECONDS)
        reader_pool.shutdown(wait=False)
        host.close()


async def spawned_status(
    argv: Sequence[str], folders: Sequence[Path]
) -> tuple[types.InitializeResult, dict[str, Any]]:
    """Start ``argv`` as a real stdio language server, the way an editor does,
    and return its ``initialize`` result and ``witan.status``.

    For the end-to-end check that a CLI entrypoint starts the host and finds
    the plugins its environment installs; :func:`lsp_session` covers behavior.
    """
    client = LanguageClient("witan-lsp-test", "0")
    logged = asyncio.Event()

    @client.feature(types.WINDOW_LOG_MESSAGE)
    def on_log(params: types.LogMessageParams) -> None:
        logged.set()

    await client.start_io(argv[0], *argv[1:])
    try:
        result = await asyncio.wait_for(
            client.initialize_async(
                types.InitializeParams(
                    capabilities=types.ClientCapabilities(),
                    workspace_folders=[
                        types.WorkspaceFolder(uri=p.as_uri(), name=p.name)
                        for p in folders
                    ],
                )
            ),
            STARTUP_TIMEOUT_SECONDS,
        )
        client.initialized(types.InitializedParams())
        await asyncio.wait_for(logged.wait(), TIMEOUT_SECONDS)
        status = await asyncio.wait_for(
            client.workspace_execute_command_async(
                types.ExecuteCommandParams(command=STATUS_COMMAND)
            ),
            TIMEOUT_SECONDS,
        )
        await asyncio.wait_for(client.shutdown_async(None), TIMEOUT_SECONDS)
        client.exit(None)
    finally:
        await asyncio.wait_for(client.stop(), TIMEOUT_SECONDS)
    return result, status
