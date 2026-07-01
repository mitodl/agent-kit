"""Setup command: install witan for supported coding agents."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from agent_config_kit import (
    InstallResult,
    apply,
    apply_all,
    detect_installed_platforms,
    known_platforms,
)
from agent_config_kit.installers import install_files

from ._common import app, console

_AGENT_NAMES = {
    "claude": "Claude Code",
    "pi": "Pi",
    "copilot": "GitHub Copilot",
    "opencode": "OpenCode",
}
AgentName = Literal["claude", "pi", "copilot", "opencode", "all"]


def _report(name: str, result: InstallResult, *, dry_run: bool) -> None:
    console.print(f"\n[bold]{_AGENT_NAMES.get(name, name)}[/bold]")
    for path in result.planned:
        tag = " [dim](dry-run)[/dim]" if dry_run else ""
        console.print(f"  [green]→[/green] {path}{tag}")
    for path, reason in result.skipped:
        console.print(f"  [yellow]skip[/yellow] {path} — {reason}")


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
    agent: Target agent — claude | pi | copilot | opencode | all. (Kilo Code is
        pending a config-path verification fix — tracked separately.)
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

    console.print("[bold]omnigraph binary[/bold]")
    su.install_omnigraph(pkg_dir, dry_run)

    bundle = su.witan_bundle(pkg_dir, author)

    if agent == "all":
        for name, result in apply_all(bundle, dry_run=dry_run).items():
            _report(name, result, dry_run=dry_run)
        for name in sorted(set(known_platforms()) - set(detect_installed_platforms())):
            console.print(
                f"\n[dim]{_AGENT_NAMES.get(name, name)} — not detected, skipping[/dim]"
            )
    else:
        _report(agent, apply(agent, bundle, dry_run=dry_run), dry_run=dry_run)

    if agent in ("claude", "all"):
        # Witan's own hook shell scripts — a generic file-copy, not part of the
        # JSON-config hook entries registered above.
        install_files(
            pkg_dir / "hooks",
            Path.home() / ".claude" / "hooks",
            suffix=".sh",
            dry_run=dry_run,
            executable=True,
        )

    if dry_run:
        console.print("\n[dim](dry-run — no files written)[/dim]")
    else:
        console.print(
            "\n[bold green]Done.[/bold green] "
            "Restart your agent(s) to pick up the new MCP server and hooks."
        )
