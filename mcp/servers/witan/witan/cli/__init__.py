"""The ``witan`` umbrella CLI.

Exposed as ``witan`` (see pyproject ``[project.scripts]``). Covers the
work-coordination graph (tasks, workflow projects, memory), starts the MCP
server (``witan serve``), and — when ``witan-code`` is installed — mounts the
code-graph tool as ``witan code …``.

It is a thin presentation layer: every query goes through the same
``witan.server`` tool functions the MCP server exposes, so behaviour
(repo scoping, ready-work computation, …) stays identical.
"""

from __future__ import annotations

from typing import Annotated, Literal

import cyclopts

from ._common import app, console
from .. import config as cfg_module
from .output import OutputFormat, set_output_format
from .run_helpers import _run_task_slug

# Import submodules to trigger @app.command / @*_app.command registrations.
from . import auth  # noqa: F401
from . import graph  # noqa: F401
from . import hooks  # noqa: F401
from . import maintenance  # noqa: F401
from . import memory  # noqa: F401
from . import projects  # noqa: F401
from . import scan  # noqa: F401
from . import session  # noqa: F401
from . import setup_cmd  # noqa: F401
from . import tasks  # noqa: F401
from . import traces  # noqa: F401
from .migrate import migrate_app

# Mount `witan migrate …` (sub-app, not a flat command).
app.command(migrate_app, name="migrate")

# Mount the code-graph CLI as `witan code …` when witan-code is installed.
# Optional: the umbrella works standalone without it.
try:
    from witan_code.cli import app as _code_app

    app.command(_code_app, name="code")
except ImportError:
    pass


@app.command
def serve(
    *,
    transport: Annotated[
        Literal["stdio", "http", "streamable-http"],
        cyclopts.Parameter(env_var="WITAN_MCP_TRANSPORT"),
    ] = "stdio",
    host: Annotated[str, cyclopts.Parameter(env_var="WITAN_MCP_HOST")] = "127.0.0.1",
    port: Annotated[int, cyclopts.Parameter(env_var="WITAN_MCP_PORT")] = 8000,
    path: Annotated[str, cyclopts.Parameter(env_var="WITAN_MCP_PATH")] = "/mcp",
) -> None:
    """Run the witan MCP server.

    Serves the work-coordination tools (memory_*, task_*, workflow_*) and, when
    witan-code is installed, mounts the code-graph tools (code_*) into the same
    server so a single MCP entry exposes everything.

    Defaults to ``stdio`` for local per-user use (Claude Desktop, ``uvx``). Pass
    ``--transport streamable-http`` (or set ``WITAN_MCP_TRANSPORT``) to expose an
    HTTP endpoint for a shared, deployed service — this is what ToolHive hosts.

    The legacy HTTP+SSE transport is not offered: MCP 2026-07-28 deprecates it
    with a 12-month offramp, and witan has no deployment on it to carry over.

    Parameters
    ----------
    transport: MCP transport. ``stdio`` for local; ``streamable-http`` (or its
        ``http`` alias) binds a network listener. Env: ``WITAN_MCP_TRANSPORT``.
    host: Interface to bind for HTTP transports. ``0.0.0.0`` inside a container.
        Env: ``WITAN_MCP_HOST``.
    port: Port to bind for HTTP transports. Env: ``WITAN_MCP_PORT``.
    path: URL path the MCP endpoint is served on (HTTP transports only).
        Env: ``WITAN_MCP_PATH``.
    """
    from ..server import mcp as witan_mcp

    try:
        from witan_code.server import mcp as code_mcp

        witan_mcp.mount(code_mcp, prefix=None)
    except ImportError:
        pass

    if transport == "stdio":
        witan_mcp.run()
    else:
        # Starlette routing asserts a leading slash; be forgiving of `mcp`.
        if not path.startswith("/"):
            path = f"/{path}"
        witan_mcp.run(transport=transport, host=host, port=port, path=path)


@app.command
def run(
    slug: str,
    *,
    target: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    claim: bool = True,
    dry_run: bool = False,
) -> None:
    """Claim a task and launch an agent to execute it.

    Claims the task (status in_progress, assignee = your author), then hands the
    terminal to ``<agent>`` seeded with a prompt describing the work. Run from
    the task's repo checkout so the agent has the right working directory.

    Parameters
    ----------
    target: Named config target to use (overrides auto-detection by repo org).
        Also overridable via WITAN_TARGET env var.
    agent: Agent CLI to launch (claude, pi, copilot, opencode, kilo). Overrides
        WITAN_AGENT env var and target/config-file default.
    model: Model passed to the agent's --model flag. Overrides WITAN_MODEL env
        var and target/config-file default.
    claim: Mark the task in_progress and assign it to you first.
    dry_run: Print the prompt and exit without launching or claiming.
    """
    try:
        cfg = cfg_module.load(target=target)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None
    _run_task_slug(
        slug, cfg=cfg, agent=agent, model=model, claim=claim, dry_run=dry_run
    )


@app.meta.default
def _launcher(
    *tokens: Annotated[str, cyclopts.Parameter(show=False, allow_leading_hyphen=True)],
    output_format: Annotated[
        OutputFormat,
        cyclopts.Parameter(name="--output-format", env_var="WITAN_OUTPUT_FORMAT"),
    ] = "txt",
) -> None:
    """witan — agent memory, planning, and collaboration graph.

    Parameters
    ----------
    output_format: Output format for table commands. Commands include tasks,
        projects, memory, traces, scan, and mounted witan-code tables. Values:
        txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT.
    """
    set_output_format(output_format)
    try:
        from witan_code.output import set_output_format as set_code_output_format

        set_code_output_format(output_format)
    except ImportError:
        pass
    app(tokens)


def main() -> None:
    from ..remote.oidc import RemoteAuthError
    from ..remote.proxy import RemoteToolUnavailable

    try:
        app.meta()
    except (RemoteAuthError, RemoteToolUnavailable) as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None
