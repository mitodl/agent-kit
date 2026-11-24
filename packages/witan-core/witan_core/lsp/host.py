"""The plugin host: folders, routes, tool providers, and merged results.

Protocol-free and stdlib only; :mod:`witan_core.lsp.server` adapts it to pygls.
Every method other than :meth:`Host.run` runs on the host's one worker thread.
The servers' synchronous dispatch runs ``asyncio.run`` per tool call, which
cannot nest inside pygls's event loop but is allowed on a plain thread, and one
thread means plugin calls never race each other over the process-wide state
the servers keep (the cwd, a module imported once).
"""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Callable, Hashable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse
from urllib.request import url2pathname

from .plugin import (
    UI_READ_TOOLS,
    Capability,
    Folder,
    Lens,
    Location,
    Note,
    Plugin,
    Route,
    escape_markdown,
    validate_plugins,
)

__all__ = [
    "HOST_COMMANDS",
    "STATUS_COMMAND",
    "UI_READ_COMMAND",
    "UI_TOOLS_COMMAND",
    "Host",
    "PluginExit",
    "disk_text",
]

STATUS_COMMAND = "witan.status"
UI_TOOLS_COMMAND = "witan.uiTools"
"""The web UI reads one folder's routes serve, each with its argument names:
what the page reads off ``tools/list`` when it speaks MCP itself."""
UI_READ_COMMAND = "witan.uiRead"
"""One web UI read, for an editor webview that hosts the page (ADR 0012's
amendment on the webview relay). Only :data:`UI_READ_TOOLS`, never a write."""
HOVER_SECTION_SEPARATOR = "\n\n---\n\n"
"""A markdown rule between two plugins' hover sections."""
HOST_COMMANDS = (STATUS_COMMAND, UI_TOOLS_COMMAND, UI_READ_COMMAND)

T = TypeVar("T")


class PluginExit(RuntimeError):
    """A plugin call raised ``SystemExit``.

    Re-raised as an ordinary error so it fails the one request. Left as
    ``SystemExit``, it would reach pygls's loop and stop the server.
    """


class BoundTools:
    """A tool provider narrowed to the tools one plugin declared.

    Unwraps a FastMCP tool to its function and runs a coroutine tool to
    completion, so plugins call every tool the same way against the in-process
    server module, the CLI's local-store guard, or a ``RemoteServerProxy``.
    Keyword arguments only, for every provider: the proxy refuses positional
    ones, so a positional call that worked against a local store would fail
    against a deployment.
    """

    def __init__(self, provider: Any, names: frozenset[str]) -> None:
        self._provider = provider
        self._names = names

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name not in self._names:
            raise AttributeError(
                f"{name!r} is not among the tools this plugin declared; see "
                "witan_core.lsp.plugin.BOUND_TOOLS"
            )
        tool = getattr(self._provider, name)
        fn = getattr(tool, "fn", tool)

        def call(*args: Any, **kwargs: Any) -> Any:
            if args:
                raise TypeError(
                    f"{name}() takes keyword arguments only; a deployment's "
                    "RemoteServerProxy refuses positional ones."
                )
            result = fn(**kwargs)
            return asyncio.run(result) if inspect.iscoroutine(result) else result

        return call

    def parameters(self, names: frozenset[str]) -> dict[str, list[str]]:
        """Each of ``names`` the provider serves, with its argument names.

        A deployment answers from its own tool list, which is the authority
        on what it serves. An in-process server answers from each tool's
        signature, leaving out the ``Context`` fastmcp injects, as its
        ``tools/list`` would. A name the provider lacks is left out: a
        deployment mounts witan-code only when it is installed there.
        """
        names = names & self._names
        # Looked up on the class: a RemoteServerProxy answers any instance
        # attribute as a tool, and the local-store guard refuses one that is
        # not a read.
        if inspect.getattr_static(type(self._provider), "remote_tools", None):
            listed = asyncio.run(self._provider.remote_tools())
            return {
                tool.name: list((tool.input_schema or {}).get("properties", {}))
                for tool in listed
                if tool.name in names
            }
        served = {}
        for name in sorted(names):
            tool = getattr(self._provider, name, None)
            if tool is None:
                continue
            served[name] = [
                param
                for param, spec in inspect.signature(
                    getattr(tool, "fn", tool)
                ).parameters.items()
                if "Context" not in str(spec.annotation)
            ]
        return served


class _Workspace:
    """The workspace snapshot both context classes give a plugin."""

    host: Host

    @property
    def folders(self) -> Sequence[Folder]:
        return list(self.host.folders.values())

    @property
    def editor_environ(self) -> Mapping[str, str]:
        return self.host.editor_environ


@dataclass(frozen=True)
class _Context(_Workspace):
    folder: Folder
    tools: Any
    host: Host

    def symbol_at(self, uri: str, line: int, character: int) -> str | None:
        return self.host.resolve_symbol(self.folder, uri, line, character)

    def text(self, uri: str) -> str:
        return self.host.document_text(uri)


@dataclass(frozen=True)
class _CommandContext(_Workspace):
    plugin: str
    host: Host

    def context(self, folder: Folder) -> _Context | None:
        return self.host.context(self.plugin, folder)


@dataclass(frozen=True)
class _Refused:
    route: Route
    anchor: Folder
    anchor_route: Route


@dataclass(frozen=True)
class _Unroutable:
    """The plugin could not route the folder: its config raised ``ValueError``
    (an undefined target, a ``remote_url`` with no issuer). Kept per folder so
    one bad target costs that folder its features, not the whole workspace."""

    reason: str


def disk_text(uri: str) -> str:
    """The file ``uri`` names, read from disk; empty when it does not exist
    (a document the client has not opened may have been deleted)."""
    try:
        return Path(url2pathname(urlparse(uri).path)).read_text(errors="replace")
    except FileNotFoundError:
        return ""


def _no_exit(fn: Callable[..., T], *args: Any) -> T:
    try:
        return fn(*args)
    except SystemExit as exc:
        raise PluginExit(f"a plugin call tried to exit ({exc.code!r})") from None


class Host:
    """Holds the plugins and the workspace folders, and answers for them."""

    def __init__(
        self,
        plugins: Mapping[str, Plugin],
        *,
        editor_environ: Mapping[str, str] = os.environ,
    ) -> None:
        validate_plugins(dict(plugins), reserved_commands=HOST_COMMANDS)
        self.plugins = dict(plugins)
        self.editor_environ = editor_environ
        """The environment the editor started this process with. ``serve()``
        passes what it had before it changed ``os.environ``; a host built
        any other way changed nothing, so it reads the live one."""
        self.folders: dict[str, Folder] = {}
        self.worker = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="witan-lsp-plugins"
        )
        self._routes: dict[tuple[str, str], Route | _Refused | _Unroutable] = {}
        self._providers: dict[tuple[str, Hashable, Hashable], BoundTools] = {}
        self._anchors: dict[str, tuple[Folder, Route]] = {}
        """Each plugin's local store: the first folder that routed to one,
        and that route. Kept for the life of the process, because the store
        stays open after its target changes or its folder is removed."""
        self._cwd_anchor: Folder | None = None
        self._cwd_pinned = False
        self.document_text: Callable[[str], str] = disk_text
        """What :meth:`Context.text` reads. The server points it at the
        editor's open buffers."""
        self.lens_fallback = False
        """The client has no CodeLens (eglot; Zed by default). The host then
        puts the resolved lenses on a hovered line into the hover, and offers
        their commands as code actions (ADR 0012's per-editor fallbacks)."""

    async def run(self, fn: Callable[..., T], /, *args: Any) -> T:
        """Run ``fn(*args)`` on the worker thread, from the event loop."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.worker, partial(_no_exit, fn, *args))

    def close(self) -> None:
        self.worker.shutdown(wait=False, cancel_futures=True)

    # ── capabilities ──────────────────────────────────────────────────────

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset().union(*(p.capabilities for p in self.plugins.values()))

    @property
    def commands(self) -> dict[str, str | None]:
        """Each command name to the plugin that owns it (``None``: the host)."""
        owned: dict[str, str | None] = dict.fromkeys(HOST_COMMANDS)
        for name, plugin in self.plugins.items():
            owned.update(dict.fromkeys(plugin.commands, name))
        return owned

    def _with(self, capability: Capability) -> list[tuple[str, Plugin]]:
        return [(n, p) for n, p in self.plugins.items() if capability in p.capabilities]

    def _serving(self) -> list[str]:
        """Plugins that give a folder anything: a capability or a command."""
        return [n for n, p in self.plugins.items() if p.capabilities or p.commands]

    # ── folders and routes ────────────────────────────────────────────────

    def set_folders(self, folders: Sequence[Folder]) -> None:
        self.folders = {}
        self._routes.clear()
        self.change_folders(folders, ())

    def change_folders(self, added: Sequence[Folder], removed: Sequence[str]) -> None:
        for uri in removed:
            self.folders.pop(uri, None)
            for key in [k for k in self._routes if k[1] == uri]:
                del self._routes[key]
        for folder in added:
            self.folders[folder.uri] = folder
        # Routed now, in workspace order, so the first folder listed anchors
        # the local store rather than whichever document is opened first.
        for folder in added:
            for name in self._serving():
                self._route(name, folder)

    def folder_for(self, path: Path | None) -> Folder | None:
        """The innermost folder containing ``path``."""
        if path is None:
            return None
        containing = [
            f
            for f in self.folders.values()
            if path == f.path or path.is_relative_to(f.path)
        ]
        return max(containing, key=lambda f: len(f.path.parts), default=None)

    def _route(self, name: str, folder: Folder) -> Route | _Refused | _Unroutable:
        """The plugin's route for ``folder``, or why this process cannot serve it.

        One process holds one local store per plugin, because both servers
        configure theirs at import. The first folder a plugin routes to a local
        store anchors that plugin's store, and a later folder whose local route
        differs gets none of its features. Later folders are compared with the
        anchor's route as it was resolved, not as its target reads now: that is
        the store already open.
        """
        key = (name, folder.uri)
        if key in self._routes:
            return self._routes[key]
        try:
            route = self.plugins[name].route(folder)
        except ValueError as exc:
            route = _Unroutable(str(exc))
        else:
            if route.local:
                anchor, anchor_route = self._anchors.setdefault(name, (folder, route))
                if self._cwd_anchor is None:
                    self._cwd_anchor = anchor
                if anchor_route.key != route.key:
                    route = _Refused(route, anchor, anchor_route)
        self._routes[key] = route
        return route

    def context(self, name: str, folder: Folder) -> _Context | None:
        route = self._route(name, folder)
        if not isinstance(route, Route):
            return None
        key = (name, route.key, route.guard)
        if key not in self._providers:
            if route.local and not self._cwd_pinned:
                # The servers resolve their local store from the cwd when their
                # module is imported, which may happen on any later call. Pin
                # it to the first anchored folder, so that is the store they
                # open. witan-code points its server at its own anchor's config
                # in open(), so its anchor may differ.
                # A relative WITAN_CONFIG named a file from the old cwd, which
                # folders were routed by; keep it naming that file.
                if config := os.environ.get("WITAN_CONFIG", "").strip():
                    os.environ["WITAN_CONFIG"] = str(
                        Path(config).expanduser().absolute()
                    )
                os.chdir(self._cwd_anchor.path)
                self._cwd_pinned = True
            plugin = self.plugins[name]
            self._providers[key] = BoundTools(plugin.open(route, folder), plugin.tools)
        return _Context(folder, self._providers[key], self)

    def _contexts(
        self, capability: Capability, path: Path | None
    ) -> list[tuple[Plugin, _Context]]:
        folder = self.folder_for(path)
        if folder is None:
            return []
        return [
            (plugin, ctx)
            for name, plugin in self._with(capability)
            if (ctx := self.context(name, folder)) is not None
        ]

    def refusals(self, folder: Folder) -> list[Note]:
        """Why plugins give this folder nothing: one note for every plugin
        refused a second local store, and one per config error."""
        routes = {name: self._route(name, folder) for name in self._serving()}
        notes = [
            Note(
                line=0, message=f"witan: no {name} features in this folder: {r.reason}"
            )
            for name, r in routes.items()
            if isinstance(r, _Unroutable)
        ]
        refused = {n: r for n, r in routes.items() if isinstance(r, _Refused)}
        if not refused:
            return notes
        anchor = next(iter(refused.values())).anchor
        stores = "; ".join(
            f"{name}: {r.route.key}, not {r.anchor_route.key}"
            for name, r in refused.items()
        )
        return [
            *notes,
            Note(
                line=0,
                message=(
                    f"witan: no {', '.join(refused)} features in this folder. It "
                    f"routes to a different local store than the one this language "
                    f"server serves for {anchor.path} ({stores}). One process "
                    "serves one local store; open this folder in its own editor "
                    "window."
                ),
            ),
        ]

    # ── merged features ───────────────────────────────────────────────────

    def resolve_symbol(
        self, folder: Folder, uri: str, line: int, character: int
    ) -> str | None:
        for name, plugin in self._with(Capability.SYMBOL_RESOLUTION):
            ctx = self.context(name, folder)
            if ctx is not None and (
                symbol := plugin.resolve_symbol(ctx, uri, line, character)
            ):
                return symbol
        return None

    def hover(
        self, path: Path | None, uri: str, line: int, character: int
    ) -> str | None:
        sections = [
            section
            for plugin, ctx in self._contexts(Capability.HOVER, path)
            if (section := plugin.hover(ctx, uri, line, character))
        ]
        if self.lens_fallback and (lenses := self._lenses_on(path, uri, line)):
            sections.append("\n\n".join(escape_markdown(lens.title) for lens in lenses))
        return HOVER_SECTION_SEPARATOR.join(sections) or None

    def code_lenses(self, path: Path | None, uri: str) -> list[Lens]:
        """Every plugin's lenses. An unresolved one carries the plugin and
        document in its ``data``, which ``codeLens/resolve`` hands back."""
        folder = self.folder_for(path)
        if folder is None:
            return []
        lenses = []
        for name, plugin in self._with(Capability.CODE_LENS):
            ctx = self.context(name, folder)
            if ctx is None:
                continue
            lenses += [
                lens
                if lens.command
                else replace(lens, data={"plugin": name, "uri": uri, "data": lens.data})
                for lens in plugin.code_lenses(ctx, uri)
            ]
        return lenses

    def resolve_lens(self, path: Path | None, line: int, data: Any) -> Lens:
        """The lens :meth:`code_lenses` sent unresolved, with its command."""
        name = data["plugin"]
        folder = self.folder_for(path)
        ctx = self.context(name, folder) if folder is not None else None
        if ctx is None:
            # The folder was removed, or stopped routing, since the lens was sent.
            return Lens(
                line=line,
                title="witan: this folder is no longer served",
                command=STATUS_COMMAND,
            )
        return self.plugins[name].resolve_lens(
            ctx, data["uri"], Lens(line=line, data=data["data"])
        )

    def code_actions(self, path: Path | None, uri: str, line: int) -> list[Lens]:
        """The lens fallback's code actions: each resolved lens on ``line``,
        whose title and command become the action's."""
        return self._lenses_on(path, uri, line) if self.lens_fallback else []

    def _lenses_on(self, path: Path | None, uri: str, line: int) -> list[Lens]:
        return [
            lens if lens.command else plugin.resolve_lens(ctx, uri, lens)
            for plugin, ctx in self._contexts(Capability.CODE_LENS, path)
            for lens in plugin.code_lenses(ctx, uri)
            if lens.line == line
        ]

    def diagnostics(self, path: Path | None, uri: str) -> list[Note]:
        folder = self.folder_for(path)
        if folder is None:
            return []
        return self.refusals(folder) + [
            note
            for plugin, ctx in self._contexts(Capability.DIAGNOSTICS, path)
            for note in plugin.diagnostics(ctx, uri)
        ]

    def definition(
        self, path: Path | None, uri: str, line: int, character: int
    ) -> list[Location]:
        return [
            loc
            for plugin, ctx in self._contexts(Capability.DEFINITION, path)
            for loc in plugin.definition(ctx, uri, line, character)
        ]

    def references(
        self,
        path: Path | None,
        uri: str,
        line: int,
        character: int,
        include_declaration: bool,
    ) -> list[Location]:
        return [
            loc
            for plugin, ctx in self._contexts(Capability.REFERENCES, path)
            for loc in plugin.references(ctx, uri, line, character, include_declaration)
        ]

    def execute(self, command: str, arguments: Sequence[Any]) -> Any:
        owner = self.commands[command]
        if owner is None:
            args: Mapping[str, Any] = arguments[0] if arguments else {}
            if command == UI_TOOLS_COMMAND:
                return self.ui_tools(args)
            if command == UI_READ_COMMAND:
                return self.ui_read(args)
            return self.status()
        return self.plugins[owner].execute_command(
            _CommandContext(owner, self), command, arguments
        )

    # ── the web UI's reads, for a webview ─────────────────────────────────

    def _ui_folder(self, args: Mapping[str, Any]) -> Folder:
        """The open folder ``args["uri"]`` names, exactly.

        Required, with no first-folder default and no containing-folder
        fallback: the folder decides which graph a webview shows. A board
        stays open after its folder is removed, and the folder holding that
        path can route to another graph, so its reads are refused instead.
        """
        uri = args.get("uri")
        if not uri:
            raise ValueError("A web UI read needs a 'uri' naming the folder.")
        folder = self.folders.get(uri)
        if folder is None:
            raise ValueError(f"{uri} is not an open workspace folder.")
        return folder

    def _ui_providers(
        self, folder: Folder
    ) -> list[tuple[Plugin, frozenset[str], _Context]]:
        """Each plugin serving ``folder`` that declared web UI reads, with
        those reads, in plugin order."""
        return [
            (plugin, reads, ctx)
            for name, plugin in self.plugins.items()
            if (reads := plugin.tools & UI_READ_TOOLS)
            and (ctx := self.context(name, folder)) is not None
        ]

    def ui_tools(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """What ``witan.uiTools`` answers: the folder's repo, and the web UI
        reads its routes serve, each with its argument names. A read two
        plugins serve is the first one's, as in :meth:`ui_read`."""
        folder = self._ui_folder(args)
        tools: dict[str, list[str]] = {}
        for plugin, reads, ctx in self._ui_providers(folder):
            with plugin.tool_calls():
                tools.update(ctx.tools.parameters(reads - tools.keys()))
        return {"repo": folder.repo, "tools": tools}

    def ui_read(self, args: Mapping[str, Any]) -> Any:
        """Run one web UI read for ``args["uri"]``'s folder, through the first
        plugin that declares the tool and serves the folder.

        The webview is the web UI, so ADR 0011's bound set is the whole of
        what it may call: anything else is refused here, whatever a plugin
        declares. The result is the tool's own value, as an in-process call
        returns it and as the remote proxy unwraps it from the MCP result.
        """
        folder = self._ui_folder(args)
        tool = args.get("tool")
        if tool not in UI_READ_TOOLS:
            raise ValueError(f"{tool!r} is not one of the web UI's reads (ADR 0011).")
        arguments = args.get("arguments") or {}
        if not isinstance(arguments, Mapping):
            raise TypeError("A web UI read's 'arguments' must be an object.")
        for plugin, reads, ctx in self._ui_providers(folder):
            if tool in reads:
                with plugin.tool_calls():
                    return getattr(ctx.tools, tool)(**arguments)
        raise ValueError(f"No witan plugin serves {tool} for {folder.path}.")

    def status(self) -> dict[str, Any]:
        """What ``witan.status`` returns: the plugins found and how each
        folder resolved, so a person can check routing from any editor."""
        return {
            "plugins": [
                {
                    "name": name,
                    "capabilities": sorted(plugin.capabilities),
                    "commands": list(plugin.commands),
                }
                for name, plugin in self.plugins.items()
            ],
            "folders": [
                {
                    "uri": f.uri,
                    "repo": f.repo,
                    "branch": f.branch,
                    "target": f.target,
                }
                for f in self.folders.values()
            ],
        }
