"""Setup command: install witan for supported coding agents."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from ._common import app, console

_AGENTS = ("claude", "pi", "copilot", "opencode", "kilo")
_AGENT_NAMES = {
    "claude": "Claude Code",
    "pi": "Pi",
    "copilot": "GitHub Copilot",
    "opencode": "OpenCode",
    "kilo": "Kilo Code",
}
AgentName = Literal["claude", "pi", "copilot", "opencode", "kilo", "all"]


@app.command
def setup(
    *,
    agent: AgentName = "claude",
    author: str | None = None,
    dry_run: bool = False,
) -> None:
    """Install witan for one or all supported coding agents.

    Installs the omnigraph binary to ``~/.local/bin/``, copies bundled skills
    and hooks/extensions to the agent's config directories, and merges the
    witan MCP server entry into the agent's config file.

    Re-run after every upgrade to refresh installed files.

    Parameters
    ----------
    agent: Target agent — claude | pi | copilot | opencode | kilo | all.
    author: Name written to graph nodes (default: git config user.name or $USER).
    dry_run: Print what would happen without writing anything.
    """
    from .. import setup as su

    pkg_dir = Path(__file__).parent.parent

    if author is None:
        try:
            author = subprocess.check_output(
                ["git", "config", "user.name"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            author = ""
        author = author or os.environ.get("USER", "unknown")

    if not shutil.which("witan") and not dry_run:
        console.print(
            "[yellow]Warning:[/yellow] witan not on PATH. "
            "Hooks calling [bold]witan inject-context[/bold] will fail until:\n"
            "  [bold]uv tool install "
            "git+https://github.com/mitodl/agent-kit"
            "#subdirectory=mcp/servers/witan[/bold]"
        )

    _installers = {
        "claude": su.install_claude,
        "pi": su.install_pi,
        "copilot": su.install_copilot,
        "opencode": su.install_opencode,
        "kilo": su.install_kilo,
    }
    _detectors = {
        "claude": lambda: True,
        "pi": su.is_pi_installed,
        "copilot": su.is_copilot_installed,
        "opencode": su.is_opencode_installed,
        "kilo": su.is_kilo_installed,
    }

    targets = list(_AGENTS) if agent == "all" else [agent]

    console.print("[bold]omnigraph binary[/bold]")
    su.install_omnigraph(pkg_dir, dry_run)

    for ag in targets:
        if agent == "all" and not _detectors[ag]():
            console.print(f"\n[dim]{_AGENT_NAMES[ag]} — not detected, skipping[/dim]")
            continue
        console.print(f"\n[bold]{_AGENT_NAMES[ag]}[/bold]")
        _installers[ag](pkg_dir, author, dry_run)

    if dry_run:
        console.print("\n[dim](dry-run — no files written)[/dim]")
    else:
        console.print(
            "\n[bold green]Done.[/bold green] "
            "Restart your agent(s) to pick up the new MCP server and hooks."
        )
