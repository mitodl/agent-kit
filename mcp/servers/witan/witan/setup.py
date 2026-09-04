"""Witan's registration bundle plus its starter-config installer.

Per-agent MCP/skill/hook installation itself lives in ``agent_config_kit``
(``apply``/``apply_all``) — this module only builds witan's own
``RegistrationBundle`` and writes witan's starter ``config.toml``. The omnigraph
binary installer is shared with witan-code and lives in
``witan_core.omnigraph_install``.
"""

from __future__ import annotations

from pathlib import Path

from agent_config_kit import (
    DeclarativeHook,
    Hook,
    HookEvent,
    PluginRegistration,
    RegistrationBundle,
    SkillSource,
    StdioServer,
)

_WITAN_ARGS = [
    "--from",
    "git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan",
    "--with",
    "git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan-code",
    "witan",
    "serve",
]

# Platforms whose witan hooks shell out to the bare `witan` command, so the CLI
# is already a hard requirement there (the setup command warns when it is
# missing). Pointing their MCP entry at that same CLI keeps both surfaces on one
# install: with the uvx form, the hooks run the PyPI release while the MCP server
# runs git `main`, and the two lineages skew silently — the agent's injected
# context and the tools it calls end up on different code with no error saying
# so. It also drops a cold-start `uvx` resolve (~90 packages) from server
# startup. The uvx form stays the default for MCP-only platforms, which have no
# persistent CLI to point at.
#
# `witan serve` mounts witan-code's `code_*` tools in-process whenever
# witan-code is importable, so the single entry still exposes everything the
# `--with`-bearing uvx form does.
_CLI_HOOK_PLATFORMS = ("claude", "pi")


def witan_bundle(pkg_dir: Path, author: str) -> RegistrationBundle:
    skills_dir = pkg_dir / "skills"
    skills = (
        [
            SkillSource(name=d.name, skill_md_path=d / "SKILL.md")
            for d in sorted(skills_dir.iterdir())
            if (d / "SKILL.md").exists()
        ]
        if skills_dir.is_dir()
        else []
    )

    pi_ext_dir = pkg_dir / "extensions" / "pi"
    # These hooks run on the prompt/stop critical path and do git + graph I/O, so
    # they carry a timeout: a hung git or graph read must degrade to no context,
    # never stall the agent. The first prompt in a cache window does several
    # full-store reads, which on a large graph can take ~10s — 15s gives that
    # cold path headroom to finish (and populate the on-disk cache) instead of
    # being killed, which would leave the cache empty and every prompt cold.
    hooks: list[Hook] = [
        DeclarativeHook(
            event=HookEvent.USER_PROMPT_SUBMIT,
            command="witan inject-context",
            timeout_seconds=15,
        ),
        DeclarativeHook(
            event=HookEvent.STOP,
            command="witan session-checkpoint",
            timeout_seconds=15,
        ),
    ]
    if pi_ext_dir.is_dir():
        hooks.extend(
            PluginRegistration(entry_path=f)
            for f in sorted(pi_ext_dir.iterdir())
            if f.suffix == ".ts"
        )

    return RegistrationBundle(
        mcp_servers={
            "witan": StdioServer(
                command="uvx", args=_WITAN_ARGS, env={"WITAN_AUTHOR": author}
            )
        },
        mcp_servers_by_platform={
            name: {
                "witan": StdioServer(
                    command="witan", args=["serve"], env={"WITAN_AUTHOR": author}
                )
            }
            for name in _CLI_HOOK_PLATFORMS
        },
        skills=skills,
        hooks=hooks,
    )


# ── Legacy hook migration ─────────────────────────────────────────────────────
# Older docs told users to register the workflow hooks as
# ``bash ~/.claude/hooks/workflow-context-inject.sh`` (a wrapper that just calls
# ``witan inject-context``). ``witan setup`` now registers the bare command, but
# Claude's hook dedup keys on the exact command string, so a pre-existing wrapper
# entry survives alongside the bare one and the context block prints twice. Prune
# any wrapper entry so the bare command is the single source of truth.

_LEGACY_HOOK_MARKERS = (
    "workflow-context-inject.sh",
    "workflow-session-checkpoint.sh",
)


def prune_legacy_hook_entries(settings: dict) -> bool:
    """Remove legacy ``.sh``-wrapper workflow-hook registrations from a Claude
    ``settings.json`` dict, in place. Returns ``True`` if anything changed.

    Matches on the wrapper script basename (an unambiguous witan-legacy marker),
    so it catches the ``bash ~/.claude/hooks/…`` and ``$REPO/configs/hooks/…``
    forms alike. Idempotent; leaves the bare ``witan …`` command entries and all
    non-witan hooks untouched. Drops a matcher entry entirely once it has no
    remaining hooks rather than leaving an empty ``"hooks": []`` behind.
    """
    hooks_section = settings.get("hooks")
    if not isinstance(hooks_section, dict):
        return False
    changed = False
    for event_name, entries in list(hooks_section.items()):
        if not isinstance(entries, list):
            continue
        kept: list = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                kept.append(entry)
                continue
            remaining = [
                h
                for h in entry["hooks"]
                if not (
                    isinstance(h, dict)
                    and any(m in (h.get("command") or "") for m in _LEGACY_HOOK_MARKERS)
                )
            ]
            if len(remaining) != len(entry["hooks"]):
                changed = True
            if remaining:
                kept.append({**entry, "hooks": remaining})
        hooks_section[event_name] = kept
    return changed


def install_default_config(dry_run: bool) -> None:
    """Write a starter ``config.toml`` if one doesn't already exist.

    Unlike the omnigraph binary (always re-fetched to the current pin), a
    config file is user-owned once created — never overwritten by a re-run,
    so `witan setup` can't clobber edits the user has already made.
    """
    from rich.console import Console

    from . import config as cfg_module

    console = Console()
    dest = cfg_module.DEFAULT_CONFIG_PATH

    if dest.exists():
        console.print(f"  [dim]config.toml[/dim] — {dest} already exists, skipping")
        return
    if dry_run:
        console.print(f"  [green]config.toml[/green] → {dest} [dim](dry-run)[/dim]")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(cfg_module.default_config_toml())
    console.print(f"  [green]config.toml[/green] → {dest}")
