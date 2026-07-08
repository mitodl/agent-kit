"""The v1 agent registry — one ``AgentPlatform`` entry per populated platform.

Per spec D7, the canonical models generalize across all 13-14 surveyed
platforms, but v1 only populates the 5 platforms witan already targets
(minus Kilo Code, whose MCP config path is unverified — see
``tk-verify-witan-install-kilo-writes-to-the-correct--5bf64e`` — so it is
intentionally absent here until that's resolved).

Built fresh on every call (not a module-level constant) so ``Path.home()`` is
read at call time — this keeps it testable via ``monkeypatch.setattr(Path,
"home", ...)``, matching the per-call ``Path.home()`` calls in the original
``witan/setup.py`` install functions.
"""

from __future__ import annotations

from pathlib import Path

from .adapters import claude as claude_adapter
from .adapters import copilot as copilot_adapter
from .adapters import opencode as opencode_adapter
from .adapters import pi as pi_adapter
from .models import AgentPlatform, CapabilityScope, ScopeTarget
from .paths import vscode_user_dir


def _registry() -> dict[str, AgentPlatform]:
    return {
        "claude": AgentPlatform(
            name="Claude Code",
            detect=lambda: True,
            mcp=CapabilityScope(
                **{
                    "global": ScopeTarget(
                        path=Path.home() / ".claude.json", key_path=("mcpServers",)
                    ),
                    # .mcp.json is Claude Code's project-root, checked-in MCP
                    # config (distinct from the per-user ~/.claude.json) —
                    # confirmed in pf-native-per-agent-skill-config-directory-
                    # hierarch-40bff8.
                    "project": ScopeTarget(
                        path=Path(".mcp.json"), key_path=("mcpServers",)
                    ),
                }
            ),
            mcp_serialize=claude_adapter.serialize_mcp,
            hooks=CapabilityScope(
                **{
                    "global": ScopeTarget(
                        path=Path.home() / ".claude" / "settings.json",
                        key_path=("hooks",),
                    ),
                    "project": ScopeTarget(
                        path=Path(".claude") / "settings.json",
                        key_path=("hooks",),
                    ),
                }
            ),
            hooks_merge=claude_adapter.merge_hooks,
            hooks_remove=claude_adapter.remove_hooks,
            skills=CapabilityScope(
                **{
                    "global": ScopeTarget(path=Path.home() / ".claude" / "skills"),
                    "project": ScopeTarget(path=Path(".claude") / "skills"),
                }
            ),
        ),
        "pi": AgentPlatform(
            name="Pi",
            detect=lambda: (Path.home() / ".pi").is_dir(),
            mcp=CapabilityScope(
                **{
                    "global": ScopeTarget(
                        path=Path.home() / ".pi" / "agent" / "mcp.json",
                        key_path=("mcpServers",),
                    ),
                    # Pi's project-root config file, unverified beyond the
                    # D-INV survey (pf-native-per-agent-skill-config-
                    # directory-hierarch-40bff8 notes project MCP paths are
                    # cwd-only but doesn't pin an exact filename) — confirm
                    # against an installed Pi version before relying on this.
                    "project": ScopeTarget(
                        path=Path(".pi") / "settings.json",
                        key_path=("mcpServers",),
                    ),
                }
            ),
            mcp_conditional_on=(
                "requires a third-party MCP plugin (pi-mcp-extension or "
                "pi-mcp-adapter); writing this file is a silent no-op without one"
            ),
            mcp_serialize=pi_adapter.serialize_mcp,
            hooks=CapabilityScope(
                **{
                    "global": ScopeTarget(
                        path=Path.home() / ".pi" / "agent" / "extensions"
                    ),
                    "project": ScopeTarget(path=Path(".pi") / "extensions"),
                }
            ),
            skills=CapabilityScope(
                **{
                    "global": ScopeTarget(
                        path=Path.home() / ".pi" / "agent" / "skills"
                    ),
                    "project": ScopeTarget(path=Path(".pi") / "skills"),
                }
            ),
            skill_dest_dirs=pi_adapter.skill_dest_dirs,
        ),
        "copilot": AgentPlatform(
            name="GitHub Copilot",
            detect=lambda: vscode_user_dir().is_dir(),
            mcp=CapabilityScope(
                **{
                    "global": ScopeTarget(
                        path=vscode_user_dir() / "mcp.json", key_path=("servers",)
                    ),
                    # .vscode/mcp.json is VS Code's workspace-scoped MCP
                    # config, distinct from the per-user file above.
                    "project": ScopeTarget(
                        path=Path(".vscode") / "mcp.json", key_path=("servers",)
                    ),
                }
            ),
            mcp_serialize=copilot_adapter.serialize_mcp,
            # No global skills target: per pf-native-per-agent-skill-config-
            # directory-hierarch-40bff8, Copilot's first-class SKILL.md
            # discovery is workspace-scoped only (.github/skills et al.),
            # with no equivalent per-user global directory surveyed.
            skills=CapabilityScope(
                **{"project": ScopeTarget(path=Path(".github") / "skills")}
            ),
        ),
        "opencode": AgentPlatform(
            name="OpenCode",
            detect=lambda: (Path.home() / ".config" / "opencode").is_dir(),
            mcp=CapabilityScope(
                **{
                    "global": ScopeTarget(
                        path=Path.home() / ".config" / "opencode" / "config.json",
                        key_path=("mcp",),
                    ),
                    # OpenCode's project-root config file — unverified exact
                    # filename beyond the D-INV survey, same caveat as the
                    # global path's own docstring; confirm during
                    # implementation of a real OpenCode integration test.
                    "project": ScopeTarget(
                        path=Path("opencode.json"), key_path=("mcp",)
                    ),
                }
            ),
            mcp_serialize=opencode_adapter.serialize_mcp,
            # OpenCode's own skill-directory naming is version-ambiguous
            # (pf-native-per-agent-skill-config-directory-hierarch-40bff8
            # notes both ".opencode/skill" and ".opencode/skills" as seen) —
            # write to both rather than guess wrong, mirroring Pi's
            # dual-dest-dir precedent (skill_dest_dirs).
            skills=CapabilityScope(
                **{"project": ScopeTarget(path=Path(".opencode") / "skill")}
            ),
            skill_dest_dirs=opencode_adapter.skill_dest_dirs,
        ),
    }


def known_platforms() -> list[str]:
    return list(_registry())


def get_platform(name: str) -> AgentPlatform:
    return _registry()[name]


def detect_installed_platforms() -> list[str]:
    return [
        name
        for name, platform in _registry().items()
        if platform.detect is None or platform.detect()
    ]
