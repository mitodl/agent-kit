"""Setup command: install witan for supported coding agents."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from ._common import app, console

_WITAN_PKG = "git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan"

_AGENTS = ("claude", "pi", "copilot", "opencode", "kilo")
_AGENT_NAMES = {
    "claude": "Claude Code",
    "pi": "Pi",
    "copilot": "GitHub Copilot",
    "opencode": "OpenCode",
    "kilo": "Kilo Code",
}
AgentName = Literal["claude", "pi", "copilot", "opencode", "kilo", "all"]


def _ensure_witan_cli(dry_run: bool) -> None:
    """Make sure the ``witan`` CLI is on PATH so the installed hooks can call it.

    The hooks invoke a bare ``witan ...`` command, but the recommended
    ``uvx ... witan setup`` install path runs witan from an ephemeral
    environment and never puts it on PATH — so the hooks fail with
    ``witan: command not found`` until the CLI is installed persistently.
    Install it with ``uv tool install`` (best-effort); fall back to a warning if
    uv is unavailable or the install fails.
    """
    if shutil.which("witan"):
        return
    if dry_run:
        console.print(
            f"  [dim]would install the witan CLI: uv tool install {_WITAN_PKG}[/dim]"
        )
        return
    if shutil.which("uv"):
        console.print("  witan CLI not on PATH — installing so hooks can call it …")
        try:
            subprocess.run(
                ["uv", "tool", "install", "--quiet", _WITAN_PKG],
                check=True,
                capture_output=True,
                text=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        else:
            if shutil.which("witan"):
                console.print("  [green]witan CLI[/green] → installed via uv tool")
            else:
                console.print(
                    "  [green]witan CLI[/green] → installed, but its bin dir is not on "
                    "PATH. Run [bold]uv tool update-shell[/bold] and restart your shell "
                    "so the hooks can find it."
                )
            return
    console.print(
        "[yellow]Warning:[/yellow] witan not on PATH. "
        "Hooks calling [bold]witan inject-context[/bold] will fail until:\n"
        f"  [bold]uv tool install {_WITAN_PKG}[/bold]"
    )


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

    _ensure_witan_cli(dry_run)

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
