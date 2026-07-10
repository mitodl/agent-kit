"""Shared state and utility functions for the witan CLI package."""

from __future__ import annotations

import asyncio
import inspect
import re
from typing import Literal

import cyclopts
from agent_config_kit import resolve_version
from rich.console import Console

from .. import repo as repo_module

# Enum literals mirrored from witan.server — drive CLI argument validation.
MemoryKind = Literal["pattern", "project_fact", "lesson", "agent_context"]
TaskType = Literal["bug", "feature", "task", "chore", "epic"]
TaskStatus = Literal["open", "in_progress", "blocked", "closed"]
TaskPriority = Literal["p0", "p1", "p2", "p3"]
TaskLinkKind = Literal["blocks", "parent", "discovered_from", "addresses"]
WorkflowPhase = Literal["discovery", "spec", "implementation", "delivery"]

app = cyclopts.App(
    name="witan",
    help="witan — agent memory, planning, and collaboration graph.",
    version=lambda: resolve_version("witan-council"),
)
console = Console()

_server = None


def _srv():
    global _server
    if _server is None:
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
