"""MCP Apps widgets (SEP-1865) for four read tools: spec §7.

A bound tool names a ``ui://`` resource in ``_meta.ui.resourceUri``. A host
that renders MCP Apps (Claude Desktop, claude.ai) fetches that resource, puts
it in a sandboxed iframe and hands it the tool's result. A host that does not
(Claude Code: anthropics/claude-code#95149) shows the text result and ignores
the rest. The binding changes nothing about the result itself, which is what
lets the second kind of host degrade to exactly today's output rather than to
nothing.

★ A BINDING EXISTS ONLY WHEN ITS WIDGET FILE DOES. The files are built by the
UI's ``npm run build`` into ``ui_dist/widgets/`` and ship in the wheel with the
page; a source install that skipped the frontend build has none. A tool whose
``resourceUri`` names a resource the server cannot serve is a broken widget in
every rendering host, so the tool is registered unbound instead. Same rule as
the ``/ui/`` routes (``ui_routes.register``), for the same reason.
"""

from __future__ import annotations

from pathlib import Path

from fastmcp.apps.config import AppConfig

from . import ui_routes

WIDGET_DIR = ui_routes.BUNDLE_DIR / "widgets"

#: The bound tools (spec §7.1). Each widget is named after its tool, so the
#: file, the URI and the tool cannot drift apart by a rename on one side.
BOUND_TOOLS = ("task_ready", "workflow_project_status", "recall", "task_list")


def widget_uri(tool: str) -> str:
    return f"ui://witan/{tool}.html"


def widget_file(tool: str, widget_dir: Path | None = None) -> Path:
    return (widget_dir or WIDGET_DIR) / f"{tool}.html"


def app_for(tool: str, widget_dir: Path | None = None) -> AppConfig | None:
    """The ``app=`` config binding ``tool`` to its widget, or ``None``.

    ``None`` when the widget was not built, so ``_tool`` registers the tool
    exactly as it would without this module.

    No ``csp`` is declared. The host then applies the extension's default,
    ``connect-src 'none'``, which is what a widget that fetches nothing wants:
    it renders the result it is handed and calls nothing back (spec §7.2).
    """
    if tool not in BOUND_TOOLS:
        msg = f"{tool} has no widget; the bound set is {BOUND_TOOLS}"
        raise ValueError(msg)
    if not widget_file(tool, widget_dir).is_file():
        return None
    return AppConfig(resource_uri=widget_uri(tool))


def register(mcp, widget_dir: Path | None = None) -> list[str]:
    """Register a ``ui://`` resource for each built widget. Returns their tools.

    Read from disk per request rather than at import. The file is a build
    output and a few hundred KB each; holding four of them in memory for a
    resource most sessions never fetch buys nothing.
    """
    registered = []
    for tool in BOUND_TOOLS:
        path = widget_file(tool, widget_dir)
        if not path.is_file():
            continue
        mcp.resource(
            widget_uri(tool),
            name=f"{tool}_widget",
            description=f"Renders the result of {tool} in an MCP Apps host.",
        )(_reader(path))
        registered.append(tool)
    return registered


def _reader(path: Path):
    # A closure rather than a default argument: fastmcp reads a resource
    # function's parameters as URI template variables.
    def read() -> str:
        return path.read_text(encoding="utf-8")

    return read
