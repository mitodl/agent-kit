"""Setup command: install witan for supported coding agents."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from agent_config_kit import (
    apply,
    apply_all,
    detect_installed_platforms,
    known_platforms,
    load_json_object,
    write_json,
)
from agent_config_kit.installers import install_files
from witan_core import install_omnigraph
from witan_core.cli import AGENT_NAMES, AgentName, report_install, resolve_author

from ._common import app, console


def _witan_code_mounted() -> bool:
    """Whether the `witan` binary actually on PATH has `witan code` mounted.

    Checked by invoking it directly rather than trusting "witan_code is
    importable in this process" alone: `witan setup` can run inside a
    different environment than the persistent `witan` binary hooks will
    later invoke (e.g. an ephemeral ``uvx --from witan --with witan-code
    witan setup`` versus a plain ``uv tool install witan`` with no
    ``--with``) — only actually running the installed binary tells us
    whether *that one* has the subcommand.
    """
    witan_bin = shutil.which("witan")
    if witan_bin is None:
        return False
    try:
        result = subprocess.run(
            [witan_bin, "code", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@app.command
def setup(
    *,
    agent: AgentName = "claude",
    author: str | None = None,
    dry_run: bool = False,
) -> None:
    """Install witan for one or all supported coding agents.

    Installs the omnigraph binary to ``~/.local/bin/``, writes a starter
    ``config.toml`` if one doesn't exist yet, copies bundled skills and
    hooks/extensions to the agent's config directories, and merges the witan
    MCP server entry into the agent's config file. When witan-code is also
    installed (importable in this environment — e.g. via ``--with`` in the
    MCP server's uvx invocation), its skill and hooks (registered as
    ``witan code …``, not a separate ``witan-code`` binary) are folded into
    the same install pass — no separate MCP entry, since ``witan serve``
    already mounts witan-code's tools in-process. A single ``witan setup``
    then covers both packages; otherwise install witan-code separately with
    ``witan-code setup`` (or the mounted ``witan code setup``).

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

    author = resolve_author(author)

    if not shutil.which("witan") and not dry_run:
        console.print(
            "[yellow]Warning:[/yellow] witan not on PATH. "
            "Hooks calling [bold]witan inject-context[/bold] will fail until:\n"
            "  [bold]uv tool install "
            "git+https://github.com/mitodl/agent-kit"
            "#subdirectory=mcp/servers/witan[/bold]"
        )

    console.print("[bold]omnigraph binary[/bold]")
    install_omnigraph(dry_run)

    console.print("\n[bold]config.toml[/bold]")
    su.install_default_config(dry_run)

    bundle = su.witan_bundle(pkg_dir, author)

    # When witan-code is installed alongside witan (e.g. via `--with` in the
    # MCP server's uvx invocation), fold its bundle in too, so one `witan
    # setup` installs both packages' skill/hooks entries — the same
    # optional-import pattern already used to mount `witan code …` and the
    # code_* MCP tools (cli/__init__.py). Standalone `witan-code setup` (or
    # the mounted `witan code setup`) still works on its own; this just saves
    # a second invocation when both are present. No cross-package import of
    # witan-code's *logic* — only its bundle-building entry point.
    #
    # Hooks register as `witan code …` (not `witan-code …`) here, so only
    # `witan` needs to be on PATH — not a second, separately installed
    # `witan-code` binary — as long as *that* `witan` was itself installed
    # with witan-code bundled in (`uv tool install --with .../witan-code
    # .../witan`; see the README's Quick Start). The MCP server entry is
    # skipped entirely: `witan serve` already mounts witan-code's code_*
    # tools in-process (cli/__init__.py's `serve` command) whenever
    # witan-code is importable — the same condition gating this whole
    # branch — so a second, standalone `witan-code` MCP server would just be
    # a redundant duplicate process exposing the same tools twice.
    try:
        import witan_code
        from witan_code.setup import witan_code_bundle
    except ImportError:
        pass
    else:
        code_pkg_dir = Path(witan_code.__file__).parent
        code_bundle = witan_code_bundle(code_pkg_dir, author, binary="witan code")
        bundle.hooks.extend(code_bundle.hooks)
        bundle.skills.extend(code_bundle.skills)

        if not dry_run and shutil.which("witan") and not _witan_code_mounted():
            console.print(
                "[yellow]Warning:[/yellow] `witan code` isn't available from "
                "the `witan` on PATH, so the hooks just registered "
                "([bold]witan code inject-context[/bold]/[bold]checkpoint[/bold]/"
                "[bold]session-init[/bold]/[bold]reindex-hook[/bold]) will "
                "fail until that `witan` has witan-code bundled in:\n"
                "  [bold]uv tool install --with "
                "git+https://github.com/mitodl/agent-kit"
                "#subdirectory=mcp/servers/witan-code "
                "git+https://github.com/mitodl/agent-kit"
                "#subdirectory=mcp/servers/witan[/bold]"
            )

    if agent == "all":
        for name, result in apply_all(bundle, dry_run=dry_run).items():
            report_install(name, result, dry_run=dry_run, console=console)
        for name in sorted(set(known_platforms()) - set(detect_installed_platforms())):
            console.print(
                f"\n[dim]{AGENT_NAMES.get(name, name)} — not detected, skipping[/dim]"
            )
    else:
        report_install(
            agent,
            apply(agent, bundle, dry_run=dry_run),
            dry_run=dry_run,
            console=console,
        )

    if agent in ("claude", "all"):
        # Witan's own hook shell scripts — a generic file-copy, not part of the
        # JSON-config hook entries registered above. witan-code has no
        # equivalent: its hooks are all bare CLI commands (witan_code/hooks.py),
        # with nothing to copy.
        install_files(
            pkg_dir / "hooks",
            Path.home() / ".claude" / "hooks",
            suffix=".sh",
            dry_run=dry_run,
            executable=True,
        )

        # Heal config drift: an older docs flow registered the workflow hooks as
        # `bash ~/.claude/hooks/workflow-*.sh` wrappers, which now coexist with
        # the bare `witan …` commands apply() just registered and make the block
        # emit twice. Prune the legacy wrapper entries so the bare command is the
        # single source of truth.
        settings_path = Path.home() / ".claude" / "settings.json"
        settings = load_json_object(settings_path)
        if settings and su.prune_legacy_hook_entries(settings):
            console.print(
                "  [yellow]pruned[/yellow] legacy workflow-hook wrapper "
                "registration(s) (were duplicating the bare `witan …` commands)"
            )
            write_json(settings_path, settings, dry_run)

    if dry_run:
        console.print("\n[dim](dry-run — no files written)[/dim]")
    else:
        console.print(
            "\n[bold green]Done.[/bold green] "
            "Restart your agent(s) to pick up the new MCP server and hooks."
        )
