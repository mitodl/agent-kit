"""Pi adapter: no "type" field on MCP entries, and skills land in both Pi's
own skills dir and the shared cross-agent pool (~/.agents/skills/).
"""

from __future__ import annotations

from pathlib import Path

from ..models import McpServer


def serialize_mcp(server: McpServer) -> dict:
    return server.model_dump(
        mode="json", exclude={"kind", "approval"}, exclude_none=True
    )


def skill_dest_dirs(primary: Path) -> list[Path]:
    return [primary, Path.home() / ".agents" / "skills"]
