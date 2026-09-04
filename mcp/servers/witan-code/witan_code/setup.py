"""Witan-code's registration bundle.

Per-agent MCP/skill/hook installation itself lives in ``agent_config_kit``
(``apply``/``apply_all``) — this module only builds witan-code's own
``RegistrationBundle``. The omnigraph binary installer is shared with witan and
lives in ``witan_core.omnigraph_install``.
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

_WITAN_CODE_ARGS = [
    "--from",
    "git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan-code",
    "witan-code",
    "serve",
]

# See ``witan.setup._CLI_HOOK_PLATFORMS`` — same reasoning, same platform set:
# these are the platforms whose hooks already invoke ``binary`` directly, so
# their MCP entry runs that same install instead of a second one resolved from
# git `main`.
_CLI_HOOK_PLATFORMS = ("claude", "pi")


def witan_code_bundle(
    pkg_dir: Path, author: str, *, binary: str = "witan-code"
) -> RegistrationBundle:
    """Build witan-code's ``RegistrationBundle``: MCP server, skill, hooks.

    Independent of ``witan``'s own bundle (``witan.setup.witan_bundle``) — a
    witan-code-only install (no witan) still gets the skill, hooks, and Pi
    extension via standalone ``witan-code setup``. When both packages are
    installed together and witan-code is importable, ``witan setup`` also
    folds this bundle in automatically (see ``witan.cli.setup_cmd``), so a
    single ``witan setup`` covers both; running ``witan-code setup``
    separately afterwards is harmless (each `apply()` call is an idempotent
    read-merge-write) but not required in that case.

    Parameters
    ----------
    binary: The command name hook entries invoke — ``"witan-code"`` for a
        standalone install (this function's default), or ``"witan code"``
        when ``witan.cli.setup_cmd`` folds this bundle into witan's own (the
        hooks then only need `witan` — with witan-code bundled in via
        ``--with`` — on PATH, not a separately installed `witan-code`).
    """
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
    # Bare CLI commands, no wrapper script — portable everywhere `binary`
    # installs (Windows included, where bash/setsid don't exist), matching
    # witan's own `witan inject-context`/`session-checkpoint` hooks. The
    # prompt-path timeouts mirror witan's: a hung git or store read must
    # degrade to no context/no compaction, never stall the agent.
    hooks: list[Hook] = [
        DeclarativeHook(
            event=HookEvent.SESSION_START,
            command=f"{binary} session-init",
        ),
        DeclarativeHook(
            event=HookEvent.POST_TOOL_USE,
            matcher="Edit|Write",
            command=f"{binary} reindex-hook",
        ),
        DeclarativeHook(
            event=HookEvent.USER_PROMPT_SUBMIT,
            command=f"{binary} inject-context",
            timeout_seconds=15,
        ),
        DeclarativeHook(
            event=HookEvent.STOP,
            command=f"{binary} checkpoint",
            timeout_seconds=15,
        ),
    ]
    if pi_ext_dir.is_dir():
        hooks.extend(
            PluginRegistration(entry_path=f)
            for f in sorted(pi_ext_dir.iterdir())
            if f.suffix == ".ts"
        )

    cli_command, *cli_args = binary.split()
    return RegistrationBundle(
        mcp_servers={
            "witan-code": StdioServer(
                command="uvx", args=_WITAN_CODE_ARGS, env={"WITAN_AUTHOR": author}
            )
        },
        mcp_servers_by_platform={
            name: {
                "witan-code": StdioServer(
                    command=cli_command,
                    args=[*cli_args, "serve"],
                    env={"WITAN_AUTHOR": author},
                )
            }
            for name in _CLI_HOOK_PLATFORMS
        },
        skills=skills,
        hooks=hooks,
    )
