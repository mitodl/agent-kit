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
                    )
                }
            ),
            mcp_serialize=claude_adapter.serialize_mcp,
            hooks=CapabilityScope(
                **{
                    "global": ScopeTarget(
                        path=Path.home() / ".claude" / "settings.json",
                        key_path=("hooks",),
                    )
                }
            ),
            hooks_merge=claude_adapter.merge_hooks,
            hooks_remove=claude_adapter.remove_hooks,
            skills=CapabilityScope(
                **{"global": ScopeTarget(path=Path.home() / ".claude" / "skills")}
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
                    )
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
                    )
                }
            ),
            skills=CapabilityScope(
                **{"global": ScopeTarget(path=Path.home() / ".pi" / "agent" / "skills")}
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
                    )
                }
            ),
            mcp_serialize=copilot_adapter.serialize_mcp,
        ),
        "opencode": AgentPlatform(
            name="OpenCode",
            detect=lambda: (Path.home() / ".config" / "opencode").is_dir(),
            mcp=CapabilityScope(
                **{
                    "global": ScopeTarget(
                        path=Path.home() / ".config" / "opencode" / "config.json",
                        key_path=("mcp",),
                    )
                }
            ),
            mcp_serialize=opencode_adapter.serialize_mcp,
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
