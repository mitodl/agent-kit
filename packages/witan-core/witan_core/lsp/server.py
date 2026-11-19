"""The witan language server over stdio, on pygls (the ``lsp`` extra).

``witan lsp`` and ``witan-code lsp`` both call :func:`serve`, so an editor runs
one witan language server whichever packages are installed. Only standard LSP
methods: features come back through hover, CodeLens, pushed diagnostics,
definition, references and ``workspace/executeCommand`` results, because Zed
cannot receive a custom method and Sublime needs a helper package for each one
(ADR 0012).
"""

from __future__ import annotations

import logging
import os
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

from lsprotocol import types
from pygls.lsp.server import LanguageServer
from pygls.uris import to_fs_path

from .folders import explicit_target, resolve_folder
from .host import Host
from .plugin import Capability, Lens, Location, Note, discover_plugins

__all__ = ["build_server", "serve"]

_AGENT_PATH_ENV_VARS = ("CLAUDE_PROJECT_DIR", "WITAN_REPO")
"""Set for an MCP server whose cwd is not the agent session's checkout. A
language server routes by its workspace folders instead, and the servers'
import-time config would otherwise resolve a different store than the folder
the host pinned the cwd to."""


def _path(uri: str) -> Path | None:
    """The file a document URI names; ``None`` for ``untitled:`` and the like,
    which belong to no workspace folder."""
    fs_path = to_fs_path(uri)
    return Path(fs_path) if fs_path else None


def _range(line: int, character: int = 0) -> types.Range:
    position = types.Position(line=line, character=character)
    return types.Range(start=position, end=position)


def _location(loc: Location) -> types.Location:
    return types.Location(uri=loc.uri, range=_range(loc.line, loc.character))


def _diagnostic(note: Note) -> types.Diagnostic:
    return types.Diagnostic(
        range=_range(note.line),
        message=note.message,
        severity=types.DiagnosticSeverity.Information,
        source="witan",
    )


def _lens(lens: Lens) -> types.CodeLens:
    return types.CodeLens(
        range=_range(lens.line),
        command=types.Command(
            title=lens.title, command=lens.command, arguments=list(lens.arguments)
        ),
    )


def _folders(params: types.InitializeParams) -> list[types.WorkspaceFolder]:
    if params.workspace_folders:
        return list(params.workspace_folders)
    if params.root_uri:
        return [types.WorkspaceFolder(uri=params.root_uri, name=params.root_uri)]
    return []


def build_server(host: Host, *, target: str | None = None) -> LanguageServer:
    """A pygls server bound to ``host``.

    Registers a handler only for what some plugin provides, since pygls
    advertises exactly the registered features, so the client is told the
    union of the plugins' capabilities and nothing more.
    """
    server = LanguageServer(
        "witan",
        metadata.version("witan-core"),
        text_document_sync_kind=types.TextDocumentSyncKind.Full,
    )
    pending: list[types.WorkspaceFolder] = []

    def resolve(folders: list[types.WorkspaceFolder]):
        return [resolve_folder(f.uri, _path(f.uri), target) for f in folders]

    @server.feature(types.INITIALIZE)
    def initialize(ls: LanguageServer, params: types.InitializeParams) -> None:
        pending.extend(_folders(params))

    @server.feature(types.INITIALIZED)
    async def initialized(ls: LanguageServer, params: types.InitializedParams) -> None:
        await host.run(lambda: host.set_folders(resolve(pending)))
        status = await host.run(host.status)
        ls.window_log_message(
            types.LogMessageParams(
                type=types.MessageType.Info,
                message=(
                    "witan: plugins "
                    + (", ".join(p["name"] for p in status["plugins"]) or "(none)")
                    + "; "
                    + "; ".join(
                        f"{f['uri']} -> target {f['target'] or '(none)'}"
                        for f in status["folders"]
                    )
                ),
            )
        )

    @server.feature(types.WORKSPACE_DID_CHANGE_WORKSPACE_FOLDERS)
    async def folders_changed(
        ls: LanguageServer, params: types.DidChangeWorkspaceFoldersParams
    ) -> None:
        event = params.event
        await host.run(
            lambda: host.change_folders(
                resolve(list(event.added)), [f.uri for f in event.removed]
            )
        )

    async def publish(ls: LanguageServer, uri: str) -> None:
        notes = await host.run(host.diagnostics, _path(uri), uri)
        ls.text_document_publish_diagnostics(
            types.PublishDiagnosticsParams(
                uri=uri, diagnostics=[_diagnostic(n) for n in notes]
            )
        )

    @server.feature(types.TEXT_DOCUMENT_DID_OPEN)
    async def did_open(ls: LanguageServer, params: types.DidOpenTextDocumentParams):
        await publish(ls, params.text_document.uri)

    @server.feature(types.TEXT_DOCUMENT_DID_SAVE)
    async def did_save(ls: LanguageServer, params: types.DidSaveTextDocumentParams):
        await publish(ls, params.text_document.uri)

    @server.feature(types.TEXT_DOCUMENT_DID_CLOSE)
    def did_close(ls: LanguageServer, params: types.DidCloseTextDocumentParams):
        ls.text_document_publish_diagnostics(
            types.PublishDiagnosticsParams(uri=params.text_document.uri, diagnostics=[])
        )

    capabilities = host.capabilities

    if Capability.HOVER in capabilities:

        @server.feature(types.TEXT_DOCUMENT_HOVER)
        async def hover(ls: LanguageServer, params: types.HoverParams):
            uri, pos = params.text_document.uri, params.position
            text = await host.run(host.hover, _path(uri), uri, pos.line, pos.character)
            if text is None:
                return None
            return types.Hover(
                contents=types.MarkupContent(kind=types.MarkupKind.Markdown, value=text)
            )

    if Capability.CODE_LENS in capabilities:

        @server.feature(types.TEXT_DOCUMENT_CODE_LENS)
        async def code_lens(ls: LanguageServer, params: types.CodeLensParams):
            uri = params.text_document.uri
            lenses = await host.run(host.code_lenses, _path(uri), uri)
            return [_lens(lens) for lens in lenses]

    if Capability.DEFINITION in capabilities:

        @server.feature(types.TEXT_DOCUMENT_DEFINITION)
        async def definition(ls: LanguageServer, params: types.DefinitionParams):
            uri, pos = params.text_document.uri, params.position
            locs = await host.run(
                host.definition, _path(uri), uri, pos.line, pos.character
            )
            return [_location(loc) for loc in locs]

    if Capability.REFERENCES in capabilities:

        @server.feature(types.TEXT_DOCUMENT_REFERENCES)
        async def references(ls: LanguageServer, params: types.ReferenceParams):
            uri, pos = params.text_document.uri, params.position
            locs = await host.run(
                host.references, _path(uri), uri, pos.line, pos.character
            )
            return [_location(loc) for loc in locs]

    def register(command: str) -> None:
        async def execute(ls: LanguageServer, *arguments: Any) -> Any:
            return await host.run(host.execute, command, arguments)

        server.command(command)(execute)

    for command in host.commands:
        register(command)

    return server


def serve(target: str | None = None) -> None:
    """Run the language server on stdio until the client exits.

    ``target`` is the CLI's ``--target``; it and ``WITAN_TARGET`` route every
    folder, as on the CLI. Raises ``ValueError`` for an undefined target,
    before the protocol starts, so the CLI can print it and exit.
    """
    explicit = explicit_target(target)
    if explicit is not None:
        # The servers read WITAN_TARGET, not the flag, when they configure
        # their local store at import.
        os.environ["WITAN_TARGET"] = explicit
    for name in _AGENT_PATH_ENV_VARS:
        os.environ.pop(name, None)

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    host = Host(discover_plugins())
    server = build_server(host, target=explicit)
    protocol_out = sys.stdout.buffer
    # stdout is the JSON-RPC channel. The writer holds the real stream, and
    # anything a plugin or a server module prints lands on stderr instead.
    original, sys.stdout = sys.stdout, sys.stderr
    try:
        server.start_io(sys.stdin.buffer, protocol_out)
    finally:
        sys.stdout = original
        host.close()
