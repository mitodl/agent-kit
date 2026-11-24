"""The witan language server over stdio, on pygls (the ``lsp`` extra).

``witan lsp`` and ``witan-code lsp`` both call :func:`serve`, so an editor runs
one witan language server whichever packages are installed. Only standard LSP
methods: features come back through hover, CodeLens, pushed diagnostics,
definition, references and ``workspace/executeCommand`` results, because Zed
cannot receive a custom method and Sublime needs a helper package for each one
(ADR 0012).
"""

from __future__ import annotations

import io
import logging
import os
import re
import sys
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from lsprotocol import types
from pygls.lsp.server import LanguageServer
from pygls.uris import to_fs_path

from .folders import explicit_target, resolve_folder
from .host import Host, disk_text
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


def _position(ls: LanguageServer, uri: str, position: types.Position):
    """``position`` in code points. A client counts columns in the encoding
    it negotiated (UTF-16 by default, UTF-8 for Neovim), and plugins index
    Python strings, so a multibyte character earlier on the line would
    otherwise shift every column after it. A document the client has not
    opened has no text to convert against, and is passed through."""
    document = _open_document(ls, uri)
    if document is None:
        return position
    return document.position_from_client_units(position)


def _open_document(ls: LanguageServer, uri: str):
    """The client's buffer for ``uri``, or ``None`` when it is not open.
    pygls keys open documents by the unquoted URI."""
    return ls.workspace.text_documents.get(unquote(uri))


def _location(loc: Location) -> types.Location:
    return types.Location(uri=loc.uri, range=_range(loc.line, loc.character))


def _diagnostic(note: Note) -> types.Diagnostic:
    return types.Diagnostic(
        range=_range(note.line),
        message=note.message,
        severity=types.DiagnosticSeverity.Information,
        source="witan",
    )


def _command(lens: Lens) -> types.Command:
    return types.Command(
        title=lens.title, command=lens.command, arguments=list(lens.arguments)
    )


def _lens(lens: Lens) -> types.CodeLens:
    if not lens.command:
        return types.CodeLens(range=_range(lens.line), data=lens.data)
    return types.CodeLens(range=_range(lens.line), command=_command(lens))


# The markdown a hover is built from: what `escape_markdown` writes (an escape,
# `&lt;` or `&amp;`), a link the plugin built around escaped text, and `**` around a
# heading word. Plugins write nothing else, and anything else passes through as
# written.
_HOVER_MARKDOWN = re.compile(
    r"\\([!-/:-@\[-`{-~])|\[((?:\\.|[^\\\]])*)\]\([^)\s]*\)|\*\*|&(lt|amp);"
)


def _plaintext(markdown: str) -> str:
    """A hover's markdown as the text it renders to, for a client that asked
    for plain text: eglot without markdown-mode shows markdown raw. A link
    keeps its text, which names the repo, path and line it points at."""

    def plain(m: re.Match[str]) -> str:
        if m[1] is not None:
            return m[1]
        if m[2] is not None:
            return _plaintext(m[2])
        return {"lt": "<", "amp": "&"}.get(m[3], "")

    return _HOVER_MARKDOWN.sub(plain, markdown)


def _hover_markup(ls: LanguageServer, markdown: str) -> types.MarkupContent:
    """Markdown unless the client lists another format first in
    ``hover.contentFormat``."""
    document = ls.client_capabilities.text_document
    formats = document and document.hover and document.hover.content_format
    if formats and formats[0] != types.MarkupKind.Markdown:
        return types.MarkupContent(
            kind=types.MarkupKind.PlainText, value=_plaintext(markdown)
        )
    return types.MarkupContent(kind=types.MarkupKind.Markdown, value=markdown)


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
        # A folder on a virtual filesystem (`vscode-vfs:`, a remote scheme) has
        # no checkout to route by, so it gets no folder and no features.
        return [
            resolve_folder(f.uri, path, target)
            for f in folders
            if (path := _path(f.uri)) is not None
        ]

    @server.feature(types.INITIALIZE)
    def initialize(ls: LanguageServer, params: types.InitializeParams) -> None:
        pending.extend(_folders(params))
        document = params.capabilities.text_document
        workspace = params.capabilities.workspace
        # lsp-mode renders lenses but advertises only `workspace.codeLens`
        # (refresh support), never `textDocument.codeLens`.
        host.lens_fallback = (document is None or document.code_lens is None) and (
            workspace is None or workspace.code_lens is None
        )
        # Not workspace.get_text_document(uri).source: for a document the
        # client has not opened it reads the file itself, and raises when the
        # file does not exist.
        host.document_text = lambda uri: (
            document.source
            if (document := _open_document(ls, uri)) is not None
            else disk_text(uri)
        )

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
            uri = params.text_document.uri
            pos = _position(ls, uri, params.position)
            text = await host.run(host.hover, _path(uri), uri, pos.line, pos.character)
            if text is None:
                return None
            return types.Hover(contents=_hover_markup(ls, text))

    if Capability.CODE_LENS in capabilities:

        @server.feature(types.TEXT_DOCUMENT_CODE_LENS)
        async def code_lens(ls: LanguageServer, params: types.CodeLensParams):
            uri = params.text_document.uri
            lenses = await host.run(host.code_lenses, _path(uri), uri)
            return [_lens(lens) for lens in lenses]

        @server.feature(types.CODE_LENS_RESOLVE)
        async def resolve_lens(ls: LanguageServer, params: types.CodeLens):
            if params.data is None:
                # Only a lens sent unresolved carries data, and it has its
                # command already otherwise.
                return params
            lens = await host.run(
                host.resolve_lens,
                _path(params.data["uri"]),
                params.range.start.line,
                params.data,
            )
            return types.CodeLens(range=params.range, command=_command(lens))

        # Registered whatever the client, since the client's capabilities
        # arrive after the handlers are; the host answers with nothing unless
        # the client has no CodeLens.
        @server.feature(types.TEXT_DOCUMENT_CODE_ACTION)
        async def code_action(ls: LanguageServer, params: types.CodeActionParams):
            uri = params.text_document.uri
            lenses = await host.run(
                host.code_actions, _path(uri), uri, params.range.start.line
            )
            # Bare commands rather than CodeActions: every client runs one
            # through workspace/executeCommand, and nothing here edits text.
            return [_command(lens) for lens in lenses]

    if Capability.DEFINITION in capabilities:

        @server.feature(types.TEXT_DOCUMENT_DEFINITION)
        async def definition(ls: LanguageServer, params: types.DefinitionParams):
            uri = params.text_document.uri
            pos = _position(ls, uri, params.position)
            locs = await host.run(
                host.definition, _path(uri), uri, pos.line, pos.character
            )
            return [_location(loc) for loc in locs]

    if Capability.REFERENCES in capabilities:

        @server.feature(types.TEXT_DOCUMENT_REFERENCES)
        async def references(ls: LanguageServer, params: types.ReferenceParams):
            uri = params.text_document.uri
            pos = _position(ls, uri, params.position)
            locs = await host.run(
                host.references,
                _path(uri),
                uri,
                pos.line,
                pos.character,
                params.context.include_declaration,
            )
            return [_location(loc) for loc in locs]

    def register(command: str) -> None:
        async def execute(ls: LanguageServer, *arguments: Any) -> Any:
            return await host.run(host.execute, command, arguments)

        server.command(command)(execute)

    for command in host.commands:
        register(command)

    return server


_OWN_LEVEL_LOGGERS = ("fastmcp",)
"""Loggers that do not inherit the root's level. fastmcp gives its logger an
INFO level and its own stderr handler when it is imported, which the witan
launcher has already done by the time :func:`serve` runs."""


def _set_log_levels() -> None:
    """Log at WARNING unless ``WITAN_LOG_LEVEL`` asks for more.

    vscode-languageclient labels every line a server writes to stderr
    ``[error]``, and pygls's ``json_rpc`` logger records each message body it
    sends at INFO, so the launcher's INFO default turned a healthy start into
    a page of errors and wrote hover text and task titles to the editor's log
    files. Only ``WITAN_LOG_LEVEL`` opts back in, not the bare ``LOG_LEVEL`` a
    shell may export for every service. The startup line reaches the editor
    as ``window/logMessage`` at Info, not through logging.
    """
    level = os.environ.get("WITAN_LOG_LEVEL", "").upper() or "WARNING"
    # A no-op under the witan launchers, which configured logging already.
    logging.basicConfig(stream=sys.stderr)
    for name in (None, *_OWN_LEVEL_LOGGERS):
        logging.getLogger(name).setLevel(level)


def serve(target: str | None = None) -> None:
    """Run the language server on stdio until the client exits.

    ``target`` is the CLI's ``--target``; it and ``WITAN_TARGET`` route every
    folder, as on the CLI. Raises ``ValueError`` for an undefined target,
    before the protocol starts, so the CLI can print it and exit.
    """
    explicit = explicit_target(target)
    # Before anything below changes it: a chat-panel agent the editor starts
    # gets this environment, not the one the plugins run under.
    editor_environ = dict(os.environ)
    if explicit is not None:
        # The servers read WITAN_TARGET, not the flag, when they configure
        # their local store at import.
        os.environ["WITAN_TARGET"] = explicit
    for name in _AGENT_PATH_ENV_VARS:
        os.environ.pop(name, None)

    _set_log_levels()
    host = Host(discover_plugins(), editor_environ=editor_environ)
    server = build_server(host, target=explicit)
    protocol_in, protocol_out = sys.stdin.buffer, sys.stdout.buffer
    # stdin and stdout are the JSON-RPC channel. The reader and writer hold
    # the real streams. Anything a plugin or a server module prints lands on
    # stderr, and stdin reads as an empty non-terminal, so no prompt
    # (`witan_core.elicit.has_terminal`) can read a request as its answer,
    # even when the server was started by hand in a terminal. Not
    # os.devnull: on Windows that is the NUL character device, whose
    # isatty() is True. A TextIOWrapper rather than a StringIO, so code that
    # reaches for sys.stdin.buffer still finds one.
    originals = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.TextIOWrapper(io.BytesIO()), sys.stderr
    try:
        server.start_io(protocol_in, protocol_out)
    finally:
        sys.stdin, sys.stdout = originals
        host.close()
