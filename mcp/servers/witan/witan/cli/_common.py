"""Shared state and utility functions for the witan CLI package."""

from __future__ import annotations

import re
from typing import Literal

import cyclopts
from rich.console import Console

from .. import repo as repo_module

# Enum literals mirrored from witan.server — drive CLI argument validation.
MemoryKind = Literal["pattern", "project_fact", "lesson", "agent_context"]
TaskType = Literal["bug", "feature", "task", "chore", "epic"]
TaskPriority = Literal["p0", "p1", "p2", "p3"]
WorkflowPhase = Literal["discovery", "spec", "implementation", "delivery"]

app = cyclopts.App(
    name="witan",
    help="witan — agent memory, planning, and collaboration graph.",
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
    """Unwrap a FastMCP-decorated tool to its plain function."""
    return getattr(tool, "fn", tool)


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
