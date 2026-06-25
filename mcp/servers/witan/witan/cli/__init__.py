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

from ._common import app, console
from .. import config as cfg_module
from .run_helpers import _run_task_slug

# Import submodules to trigger @app.command / @*_app.command registrations.
from . import hooks  # noqa: F401
from . import memory  # noqa: F401
from . import projects  # noqa: F401
from . import setup_cmd  # noqa: F401
from . import tasks  # noqa: F401

# Mount the code-graph CLI as `witan code …` when witan-code is installed.
# Optional: the umbrella works standalone without it.
try:
    from witan_code.cli import app as _code_app

    app.command(_code_app, name="code")
except ImportError:
    pass


@app.command
def serve() -> None:
    """Run the witan MCP server.

    Serves the work-coordination tools (memory_*, task_*, workflow_*) and, when
    witan-code is installed, mounts the code-graph tools (code_*) into the same
    server so a single MCP entry exposes everything.
    """
    from ..server import mcp as witan_mcp

    try:
        from witan_code.server import mcp as code_mcp

        witan_mcp.mount(code_mcp, prefix=None)
    except ImportError:
        pass
    witan_mcp.run()


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


def main() -> None:
    app()
