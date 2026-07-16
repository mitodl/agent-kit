"""Shared CLI scaffolding for the witan servers' ``setup`` commands.

Uses the ``cli`` extra: ``make_app`` needs cyclopts, and ``report_install``
uses agent-config-kit's ``InstallResult`` type. This module never imports rich
itself — the extra bundles it only for ``report_install``'s styled branch,
which runs on a rich ``Console`` the *caller* passes in (without one it plain
``print``s), so importing/using ``witan_core.cli`` never requires rich.
Following the package convention (see ``__init__``), nothing here is re-exported
from the root package — import from ``witan_core.cli`` directly.

The pieces both ``witan`` (witan-council) and ``witan_code`` carried as
copy-paste duplicates: the supported-agent constants, the ``--version`` app
factory, ``--author`` resolution, and the install-result printer. Everything
else in each server's ``setup`` command is package-local (different bundles,
different post-install healing) and stays put.

``report_install`` deliberately renders plain text without a ``console`` so
witan-code's setup can call it without importing rich just for that line.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    import cyclopts
    from agent_config_kit import InstallResult
    from rich.console import Console

# The coding agents witan supports installing into. Both servers' `setup`
# commands mirror this literal for their `--agent` argument; "all" fans out
# across every detected platform.
AgentName = Literal["claude", "pi", "copilot", "opencode", "all"]

# Human-readable labels keyed by the AgentName literals (minus "all"), used in
# install reports. `.get(name, name)` falls back to the raw key.
AGENT_NAMES: dict[str, str] = {
    "claude": "Claude Code",
    "pi": "Pi",
    "copilot": "GitHub Copilot",
    "opencode": "OpenCode",
}


def make_app(*, name: str, help_text: str, version_dist: str) -> cyclopts.App:
    """Build the top-level cyclopts ``App`` for a witan CLI.

    ``version_dist`` is the installed distribution name whose version
    ``--version`` reports (e.g. ``"witan-council"`` or ``"witan-code"``).
    """
    import cyclopts
    from agent_config_kit.version import resolve_version

    return cyclopts.App(
        name=name,
        help=help_text,
        version=lambda: resolve_version(version_dist),
    )


def resolve_author(author: str | None) -> str:
    """Resolve the graph-attribution author for a ``setup`` run.

    An explicit value wins; otherwise fall back to ``git config user.name``,
    then ``$USER``, then ``"unknown"``.
    """
    if author is not None:
        return author
    try:
        resolved = subprocess.check_output(
            ["git", "config", "user.name"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        # OSError covers a missing git binary (FileNotFoundError) plus any other
        # OS-level failure to spawn it (e.g. PermissionError); degrade to the
        # $USER/"unknown" fallback rather than letting `setup` crash.
        resolved = ""
    return resolved or os.environ.get("USER", "unknown")


def report_install(
    name: str,
    result: InstallResult,
    *,
    dry_run: bool,
    console: Console | None = None,
) -> None:
    """Print an agent-config ``InstallResult``.

    With a rich ``console`` the output is styled (witan-council's look); without
    one it is plain ``print`` (witan-code's look). Keeping both branches here
    lets the two servers share the printer without witan-code taking on rich for
    it.
    """
    label = AGENT_NAMES.get(name, name)
    if console is not None:
        console.print(f"\n[bold]{label}[/bold]")
        for path in result.planned:
            tag = " [dim](dry-run)[/dim]" if dry_run else ""
            console.print(f"  [green]→[/green] {path}{tag}")
        for path, reason in result.skipped:
            console.print(f"  [yellow]skip[/yellow] {path} — {reason}")
    else:
        print(f"\n{label}")
        for path in result.planned:
            tag = " (dry-run)" if dry_run else ""
            print(f"  -> {path}{tag}")
        for path, reason in result.skipped:
            print(f"  skip {path} — {reason}")
