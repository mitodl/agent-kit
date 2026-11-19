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
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, TypeVar

from .plugin import (
    Capability,
    Folder,
    Lens,
    Location,
    Note,
    Plugin,
    Route,
    validate_plugins,
)

__all__ = ["HOST_COMMANDS", "STATUS_COMMAND", "Host", "PluginExit"]

STATUS_COMMAND = "witan.status"
HOST_COMMANDS = (STATUS_COMMAND,)

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


@dataclass(frozen=True)
class _Context:
    folder: Folder
    tools: Any
    host: Host

    def symbol_at(self, uri: str, line: int, character: int) -> str | None:
        return self.host.resolve_symbol(self.folder, uri, line, character)


@dataclass(frozen=True)
class _CommandContext:
    plugin: str
    host: Host

    @property
    def folders(self) -> Sequence[Folder]:
        return list(self.host.folders.values())

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


def _no_exit(fn: Callable[..., T], *args: Any) -> T:
    try:
        return fn(*args)
    except SystemExit as exc:
        raise PluginExit(f"a plugin call tried to exit ({exc.code!r})") from None


class Host:
    """Holds the plugins and the workspace folders, and answers for them."""

    def __init__(self, plugins: Mapping[str, Plugin]) -> None:
        validate_plugins(dict(plugins), reserved_commands=HOST_COMMANDS)
        self.plugins = dict(plugins)
        self.folders: dict[str, Folder] = {}
        self.worker = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="witan-lsp-plugins"
        )
        self._routes: dict[tuple[str, str], Route | _Refused | _Unroutable] = {}
        self._providers: dict[tuple[str, Hashable], BoundTools] = {}
        self._anchors: dict[str, tuple[Folder, Route]] = {}
        """Each plugin's local store: the first folder that routed to one,
        and that route. Kept for the life of the process, because the store
        stays open after its target changes or its folder is removed."""
        self._cwd_anchor: Folder | None = None
        self._cwd_pinned = False

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
            for name, plugin in self.plugins.items():
                if plugin.capabilities:
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
        key = (name, route.key)
        if key not in self._providers:
            if route.local and not self._cwd_pinned:
                # The servers resolve their local store from the cwd when their
                # module is imported, which may happen on any later call. Pin
                # it to the first anchored folder, so that is the store they
                # open. witan-code points its server at its own anchor's config
                # in open(), so its anchor may differ.
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
        routes = {
            name: self._route(name, folder)
            for name, plugin in self.plugins.items()
            if plugin.capabilities
        }
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
        return "\n\n---\n\n".join(sections) or None

    def code_lenses(self, path: Path | None, uri: str) -> list[Lens]:
        return [
            lens
            for plugin, ctx in self._contexts(Capability.CODE_LENS, path)
            for lens in plugin.code_lenses(ctx, uri)
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
        self, path: Path | None, uri: str, line: int, character: int
    ) -> list[Location]:
        return [
            loc
            for plugin, ctx in self._contexts(Capability.REFERENCES, path)
            for loc in plugin.references(ctx, uri, line, character)
        ]

    def execute(self, command: str, arguments: Sequence[Any]) -> Any:
        owner = self.commands[command]
        if owner is None:
            return self.status()
        return self.plugins[owner].execute_command(
            _CommandContext(owner, self), command, arguments
        )

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
