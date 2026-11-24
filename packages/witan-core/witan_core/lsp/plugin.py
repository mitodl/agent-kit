"""The witan language server's plugin interface.

ADR 0012 ("2. The language server") puts the protocol in a host here and the
features in plugins that witan-council and witan-code register under the
``witan.lsp.plugins`` entry-point group, so one server process serves whichever
of the two packages is installed.

stdlib only. A server package's plugin module imports this at module scope, and
``just check-core-floor`` imports every module of each server in a clean
environment without the ``lsp`` extra. Plugins return the plain records below
and the host turns them into LSP messages, so no plugin imports pygls either.
"""

from __future__ import annotations

import re
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from importlib import metadata
from pathlib import Path
from typing import Any, Protocol

__all__ = [
    "BOUND_TOOLS",
    "ENTRY_POINT_GROUP",
    "UI_READ_TOOLS",
    "Capability",
    "CommandContext",
    "Context",
    "Folder",
    "Lens",
    "Location",
    "Note",
    "Plugin",
    "Route",
    "discover_plugins",
    "escape_markdown",
    "validate_plugins",
]

ENTRY_POINT_GROUP = "witan.lsp.plugins"

UI_READ_TOOLS = frozenset(
    {
        "task_ready",
        "task_get",
        "task_list",
        "task_search",
        "workflow_project_get",
        "workflow_project_status",
        "workflow_project_list",
        "workflow_session_list",
        "recall",
        "memory_get",
        "memory_list",
        "memory_search",
        "memory_contradictions",
        "memory_stale_refs",
        "code_repo_dependencies",
        "code_interface_providers",
        "code_interface_consumers",
    }
)
"""ADR 0011's bound set: every read the web UI makes, and nothing else.

The host relays these, and only these, for an editor webview that hosts the
web UI (``witan.uiRead``, ADR 0012's amendment on the webview relay). The
webview is the web UI, so ADR 0011 binds it, not this server's own binding.
witan-council's ``witan.ui_access.READ_TOOLS`` is the same set (a test there
holds the two equal); it cannot be imported from here, because witan-core
depends on neither server.
"""

BOUND_TOOLS = frozenset(
    {
        # The feature map in ADR 0012.
        "code_callers",
        "code_cross_repo_impact",
        "code_interface_bindings_in_file",
        "code_interface_providers",
        "code_interface_consumers",
        "code_find_definition",
        "code_find_references",
        "code_indexed_branches",
        "memory_stale_refs",
        "recall",
        "task_ready",
        "task_for_branch",
        "workflow_project_list",
        "task_list",
        # Symbol resolution, which the ADR has witan-code provide from this tool.
        "code_symbols_in_file",
        # The launch contract.
        "task_get",
        "workflow_project_get",
        "task_claim",
        "task_release",
        # A project launch's prompt memories (``witan.cli_only``).
        "workflow_project_run_memories",
        # The web UI's reads, which the host relays for a webview.
        *UI_READ_TOOLS,
    }
)
"""Every tool the language server may call, as ADR 0012 enumerates them.

It includes :data:`UI_READ_TOOLS`, which the host relays for a webview. A
plugin declares the subset it uses in :attr:`Plugin.tools`, and the host
refuses to load one that names a tool outside this set. Widening it amends the
ADR, the same rule ADR 0011 sets for the web UI's binding.
"""


class Capability(StrEnum):
    """What a plugin provides. The host advertises the union to the client."""

    HOVER = "hover"
    CODE_LENS = "code_lens"
    DIAGNOSTICS = "diagnostics"
    DEFINITION = "definition"
    REFERENCES = "references"
    SYMBOL_RESOLUTION = "symbol_resolution"
    """Mapping a document position to a witan-code symbol id. Plugins with
    symbol-scoped features ask :meth:`Context.symbol_at`, which answers
    ``None`` when no installed plugin provides this."""


@dataclass(frozen=True)
class Folder:
    """A workspace folder, resolved once when the client reports it."""

    uri: str
    path: Path
    repo: str | None
    """Canonical repo key from the folder's own git remote."""
    branch: str | None
    """The raw branch checked out in the folder."""
    target: str | None
    """The ``[targets.<name>]`` block that routes this folder, or ``None``."""


@dataclass(frozen=True)
class Route:
    """Where one plugin's reads for one folder go.

    The host opens one tool provider per distinct ``key`` and ``guard``, so
    folders that share a deployment share its ``RemoteServerProxy``. A process
    holds one local store, so ``local`` routes with different keys cannot both
    be served.
    """

    key: Hashable
    """The store. Shown in the diagnostic on a folder refused a second one."""
    local: bool
    guard: Hashable = None
    """What else the provider depends on besides the store. Folders on one
    store whose guards differ share the store but each get their own
    provider: witan-council refuses writes to a local store nothing selected,
    and must not lend that refusal, or its absence, to another folder."""


@dataclass(frozen=True)
class Lens:
    """A CodeLens on one line.

    A plugin whose lens is costly to compute returns it with ``data`` and no
    ``command``: the host sends it unresolved and calls
    :meth:`Plugin.resolve_lens` only for the lenses the client asks
    ``codeLens/resolve`` about (the ones on screen). ``data`` must be
    JSON-serialisable, since it travels to the client and back.
    """

    line: int
    title: str = ""
    command: str = ""
    arguments: tuple[Any, ...] = ()
    data: Any = None


@dataclass(frozen=True)
class Note:
    """An Information diagnostic on one line of the document."""

    line: int
    message: str


@dataclass(frozen=True)
class Location:
    uri: str
    line: int
    character: int = 0


class Context(Protocol):
    """What a plugin call gets for the folder its document belongs to."""

    folder: Folder
    tools: Any
    """The plugin's tool provider for this folder, narrowed to
    :attr:`Plugin.tools`. Tools are plain synchronous callables that take
    keyword arguments only, as a deployment's ``RemoteServerProxy`` does."""

    @property
    def folders(self) -> Sequence[Folder]:
        """Every workspace folder, so a plugin can tell whether a location in
        another repo is checked out in this workspace."""
        ...

    @property
    def editor_environ(self) -> Mapping[str, str]:
        """The environment the editor started the server with.

        ``serve()`` removes ``CLAUDE_PROJECT_DIR`` and ``WITAN_REPO``, and sets
        ``WITAN_TARGET`` from ``--target``, before any plugin runs, so
        ``os.environ`` no longer shows what the editor has. Anything the editor
        starts itself (a chat-panel agent) gets this environment instead.
        """
        ...

    def symbol_at(self, uri: str, line: int, character: int) -> str | None: ...

    def text(self, uri: str) -> str:
        """The document's text: the editor's buffer when the document is open,
        else the file on disk, else empty."""
        ...


class CommandContext(Protocol):
    """What :meth:`Plugin.execute_command` gets: every folder, and their contexts."""

    folders: Sequence[Folder]

    @property
    def editor_environ(self) -> Mapping[str, str]:
        """As :attr:`Context.editor_environ`."""
        ...

    def context(self, folder: Folder) -> Context | None:
        """``None`` when the folder routes to a local store this process does
        not serve."""
        ...


class Plugin:
    """Base class for a language-server plugin.

    Override only what :attr:`capabilities` declares. Every method runs on the
    host's single worker thread, so a call may block and may run its own
    ``asyncio`` loop, and must not write stdout (the JSON-RPC channel) or
    raise ``SystemExit``.
    """

    capabilities: frozenset[Capability] = frozenset()
    commands: tuple[str, ...] = ()
    """``witan.*`` command names, advertised in ``executeCommandProvider``."""
    tools: frozenset[str] = frozenset()

    def route(self, folder: Folder) -> Route:
        raise NotImplementedError

    def open(self, route: Route, folder: Folder) -> Any:
        """Return the tool provider for ``route``; ``folder`` is the first one
        routed there."""
        raise NotImplementedError

    def hover(self, ctx: Context, uri: str, line: int, character: int) -> str | None:
        """One markdown section of the merged hover."""
        return None

    def code_lenses(self, ctx: Context, uri: str) -> list[Lens]:
        return []

    def resolve_lens(self, ctx: Context, uri: str, lens: Lens) -> Lens:
        """Fill in the title and command of a lens returned with only ``data``."""
        return lens

    def diagnostics(self, ctx: Context, uri: str) -> list[Note]:
        return []

    def definition(
        self, ctx: Context, uri: str, line: int, character: int
    ) -> list[Location]:
        return []

    def references(
        self,
        ctx: Context,
        uri: str,
        line: int,
        character: int,
        include_declaration: bool,
    ) -> list[Location]:
        """``include_declaration`` is the request's ``context.includeDeclaration``."""
        return []

    def resolve_symbol(
        self, ctx: Context, uri: str, line: int, character: int
    ) -> str | None:
        return None

    def execute_command(
        self, ctx: CommandContext, command: str, arguments: Sequence[Any]
    ) -> Any:
        raise NotImplementedError(command)


# Every ASCII punctuation character, which is exactly the set CommonMark lets
# a backslash escape.
_MARKDOWN_SPECIAL = re.compile(r"([!-/:-@\[-`{-~])")


def escape_markdown(text: str) -> str:
    """``text`` as literal markdown: no link, image, emphasis, entity or HTML.

    For text a plugin does not control (teammate-written titles, keys and
    paths read from the graph), which ADR 0012 has the server render as plain
    text inside its markdown. Every ASCII punctuation character is escaped:
    ``:`` stops a bare URL from autolinking and ``&`` an entity. Leading and
    trailing whitespace is dropped and every inner run collapses to one space,
    so no line starts a heading, list or fence, and no indent starts a code
    block.
    """
    return _MARKDOWN_SPECIAL.sub(r"\\\1", " ".join(text.split()))


def discover_plugins() -> dict[str, Plugin]:
    """Instantiate every installed plugin, keyed and ordered by entry-point name.

    The order is the fixed order the host merges CodeLens lists and hover
    sections in. A plugin that fails to load raises: an editor showing half
    the features with no error is worse than a server that says why it did
    not start.
    """
    plugins = {}
    for ep in sorted(
        metadata.entry_points(group=ENTRY_POINT_GROUP), key=lambda e: e.name
    ):
        try:
            factory = ep.load()
        except Exception as exc:
            raise RuntimeError(
                f"Failed to load language-server plugin {ep.name!r} ({ep.value}): {exc}"
            ) from exc
        plugins[ep.name] = factory()
    return plugins


def validate_plugins(
    plugins: dict[str, Plugin], *, reserved_commands: Sequence[str]
) -> None:
    """Refuse a plugin that is not a :class:`Plugin`, or names a command or
    tool outside its lane."""
    seen = set(reserved_commands)
    for name, plugin in plugins.items():
        if not isinstance(plugin, Plugin):
            raise TypeError(f"Language-server plugin {name!r} is not a Plugin.")
        for capability in plugin.capabilities:
            Capability(capability)
        for command in plugin.commands:
            if not command.startswith("witan."):
                raise ValueError(
                    f"Plugin {name!r} command {command!r} is not in the witan.* namespace."
                )
            if command in seen:
                raise ValueError(
                    f"Plugin {name!r} command {command!r} is registered twice."
                )
            seen.add(command)
        if unbound := plugin.tools - BOUND_TOOLS:
            raise ValueError(
                f"Plugin {name!r} uses tools outside the language server's binding "
                f"(ADR 0012): {', '.join(sorted(unbound))}."
            )
