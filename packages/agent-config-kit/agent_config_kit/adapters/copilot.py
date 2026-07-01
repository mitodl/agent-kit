"""GitHub Copilot / VS Code adapter: requires a literal "type" field, same as
Claude Code, on entries under <vscode-user-dir>/mcp.json's "servers" key.
"""

from __future__ import annotations

from ..models import McpServer


def serialize_mcp(server: McpServer) -> dict:
    data = server.model_dump(
        mode="json", exclude={"kind", "approval"}, exclude_none=True
    )
    data["type"] = server.kind
    return data
