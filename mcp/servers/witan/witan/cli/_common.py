"""Shared state and utility functions for the witan CLI package."""

from __future__ import annotations

import asyncio
import inspect
import re
from typing import Literal

from rich.console import Console
from rich.table import Table
from witan_core.cli import make_app

from .. import repo as repo_module
from .output import dump_structured, get_output_format

# Enum literals mirrored from witan.server — drive CLI argument validation.
MemoryKind = Literal["pattern", "project_fact", "lesson", "agent_context"]
TaskType = Literal["bug", "feature", "task", "chore", "epic"]
TaskStatus = Literal["open", "in_progress", "blocked", "closed"]
TaskPriority = Literal["p0", "p1", "p2", "p3"]
TaskLinkKind = Literal["blocks", "parent", "discovered_from", "addresses"]
WorkflowPhase = Literal["discovery", "spec", "implementation", "delivery"]

app = make_app(
    name="witan",
    help_text="witan — agent memory, planning, and collaboration graph.",
    version_dist="witan-council",
)
console = Console()

_server = None


def _srv():
    """Return the tool provider the CLI dispatches through.

    In-process ``witan.server`` by default; a network-dispatching
    ``RemoteServerProxy`` when ``WITAN_REMOTE_URL`` is set (ADR-0005). The
    proxy mirrors the server module's tool surface, so every call site is
    identical either way.
    """
    global _server
    if _server is None:
        from .. import config as cfg_module

        # A misconfigured remote (e.g. WITAN_REMOTE_URL without WITAN_OIDC_ISSUER)
        # raises ValueError here; surface it as a clean CLI error rather than
        # letting a traceback escape every command that touches the graph.
        try:
            remote = cfg_module.load_remote_config()
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise SystemExit(1) from None
        if remote is not None:
            from ..remote.oidc import default_token_provider, default_token_refresher
            from ..remote.proxy import RemoteServerProxy

            _server = RemoteServerProxy(
                remote,
                default_token_provider(remote),
                default_token_refresher(remote),
            )
        else:
            from .. import server as server_module

            _server = server_module
    return _server


def _fn(tool):
    """Unwrap a FastMCP-decorated tool to a directly-callable function.

    Tools that gained MCP elicitation are ``async def`` (they take a
    ``ctx: Context`` FastMCP injects). The CLI calls them directly, not through
    an MCP client, so wrap a coroutine tool to run to completion via
    ``asyncio.run`` — with no ctx it falls back to its non-interactive default,
    which is the right behavior for a plain ``witan …`` command.
    """
    fn = getattr(tool, "fn", tool)
    if inspect.iscoroutinefunction(fn):

        def runner(*args, **kwargs):
            return asyncio.run(fn(*args, **kwargs))

        return runner
    return fn


def _repo_arg(repo: str | None, all_repos: bool) -> str | None:
    """Map --repo/--all-repos to the server tools' ``repo`` parameter.

    ``""`` means all repos; ``None`` means detect the current repo; a string
    scopes to that repo.
    """
    return "" if all_repos else repo


def _split_csv(items: list[str] | None) -> list[str] | None:
    if items is None:
        return None
    return [x.strip() for item in items for x in item.split(",") if x.strip()] or None


_PRIORITY_STYLE = {"p0": "bold red", "p1": "red", "p2": "yellow", "p3": "dim"}
_STATUS_STYLE = {
    "open": "green",
    "in_progress": "cyan",
    "blocked": "red",
    "closed": "dim",
    "active": "green",
    "completed": "blue",
    "abandoned": "dim",
}


def _styled(value: str, table: dict) -> str:
    style = table.get(value)
    return f"[{style}]{value}[/{style}]" if style else (value or "")


def _short_repo(uri: str | None) -> str:
    """Strip scheme+host from a repo URI for compact display."""
    if not uri:
        return ""
    m = re.match(r"https?://[^/]+/(.+)", uri)
    return m.group(1) if m else uri


def _detect_repo_for_display() -> str | None:
    """Detect current repo URI for CLI display/filtering."""
    return repo_module.detect()


def render_table(
    *,
    title: str,
    columns: list[str],
    rows: list[dict[str, object]],
    no_wrap: set[str] | None = None,
    styles: dict[str, dict[str, str]] | None = None,
    dim_if_present: set[str] | None = None,
    placeholders: dict[str, str] | None = None,
) -> None:
    """Render ``rows`` as a rich table, or dump them structured per ``--output-format``.

    ``rows`` hold plain, unstyled values of any JSON/TOML/YAML-serializable
    type — ``None`` is normalized to ``""`` (TOML has no null, and a bare
    ``None`` would otherwise print as the literal text ``"None"``); every
    other type (``int``, ``float``, ``bool``, ``str``) passes through
    unchanged, so structured output keeps its native type (e.g. a session
    count stays a JSON number). ``styles``/``dim_if_present``/``placeholders``
    are display-only concerns applied only in ``txt`` mode.
    """
    rows = [{k: ("" if v is None else v) for k, v in r.items()} for r in rows]

    fmt = get_output_format()
    if fmt != "txt":
        dump_structured(rows, title, fmt)
        return

    no_wrap = no_wrap or set()
    styles = styles or {}
    dim_if_present = dim_if_present or set()
    placeholders = placeholders or {}

    table = Table(title=title, header_style="bold")
    for col in columns:
        if col in no_wrap:
            table.add_column(col, no_wrap=True)
        else:
            table.add_column(col, overflow="fold", no_wrap=False)

    for r in rows:
        cells = []
        for col in columns:
            value = str(r.get(col, ""))
            if not value and col in placeholders:
                cells.append(f"[dim]{placeholders[col]}[/dim]")
            elif col in styles:
                cells.append(_styled(value, styles[col]))
            elif value and col in dim_if_present:
                cells.append(f"[dim]{value}[/dim]")
            else:
                cells.append(value)
        table.add_row(*cells)
    console.print(table)
