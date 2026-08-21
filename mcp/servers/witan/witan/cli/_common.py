"""Shared state and utility functions for the witan CLI package."""

from __future__ import annotations

import asyncio
import inspect
import re
from typing import Literal

from rich.console import Console
from rich.markup import escape
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

# ★ STARTUP DIAGNOSTICS MUST NOT GO TO STDOUT.
# Under `witan serve`'s default stdio transport, stdout IS the JSON-RPC
# channel — anything printed there is a non-protocol line in the middle of the
# stream, which can stop the client completing MCP initialization. That makes
# the ordinary `console` above unusable for exactly the messages that matter
# most: the ones explaining why the server is refusing to start.
stderr_console = Console(stderr=True)

_server = None


def _srv():
    """Return the tool provider the CLI dispatches through.

    In-process ``witan.server`` by default; a network-dispatching
    ``RemoteServerProxy`` when ``WITAN_REMOTE_URL`` is set (ADR-0005). The
    proxy mirrors the server module's tool surface, so every call site is
    identical either way.

    The in-process branch goes through ``local_server``, which guards it when a
    deployment is configured but nothing routed this invocation to it — see
    ``witan.cli.local_dispatch``. Falling through to the local store there is
    how a task close reported success against a graph nobody was reading.

    ★ THE DIAGNOSIS RUNS BEFORE ``witan.server`` IS IMPORTED, and the order is
    load-bearing rather than tidy. That import calls ``_ensure_graph`` at module
    scope, which creates a missing local store and re-applies its schema — so
    importing first and refusing afterwards would have already written to the
    graph the refusal exists to protect. ``local_server`` receives the import as
    a callable and only runs it if a read is actually allowed.
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
            print_error(exc)
            raise SystemExit(1) from None
        if remote is not None:
            _server = remote_proxy(remote)
        else:
            from .local_dispatch import local_server

            _server = local_server(cfg_module.diagnose_local_dispatch())
    return _server


def remote_proxy(remote):
    """Build the network-dispatching proxy for one resolved ``RemoteConfig``.

    Separate from ``_srv()`` because the destination is not always the ambient
    one: ``witan migrate merge --to <name>`` names a deployment on the command
    line and builds its proxy directly.
    """
    from ..remote.oidc import default_token_provider, default_token_refresher
    from ..remote.proxy import RemoteServerProxy

    return RemoteServerProxy(
        remote,
        default_token_provider(remote),
        default_token_refresher(remote),
    )


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


def esc(value: object) -> str:
    """Escape stored graph content for interpolation into a markup string.

    Rich reads square brackets in ``Console.print`` as style tags, so text
    holding a TOML section (``[targets.production]``), a Python repr, or a
    markdown link renders with that substring silently dropped — a resolution
    that named which target was misconfigured turns into one that names none.
    Nothing indicates anything was removed.

    Every renderer that prints stored content goes through this or
    :func:`render_table`, which applies it per cell. Escape at the boundary
    where graph text meets markup, not at each call site: agent-kit#261 fixed
    two sites that way and every other one stayed broken.
    """
    return escape("" if value is None else str(value))


def print_error(message: object, *, stderr: bool = False) -> None:
    """Print ``message`` in red, escaped.

    Error text is the worst place to drop a bracketed substring: witan's own
    refusals name the config section to fix (``[targets.production]``), and an
    omnigraph error quotes the query it choked on. Both are markup to Rich.
    """
    (stderr_console if stderr else console).print(f"[red]{esc(message)}[/red]")


def _styled(value: str, table: dict) -> str:
    style = table.get(value)
    return f"[{style}]{esc(value)}[/{style}]" if style else esc(value)


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
                cells.append(f"[dim]{esc(placeholders[col])}[/dim]")
            elif col in styles:
                cells.append(_styled(value, styles[col]))
            elif value and col in dim_if_present:
                cells.append(f"[dim]{esc(value)}[/dim]")
            else:
                cells.append(esc(value))
        table.add_row(*cells)
    console.print(table)
