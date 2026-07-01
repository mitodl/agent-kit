"""Pi adapter: no "type" field on MCP entries — stdio and remote servers share
one shape distinguished by presence of "command" vs "url" (see
adapters/_wire/pi_mcp.py). Skills land in both Pi's own skills dir and the
shared cross-agent pool (~/.agents/skills/).
"""

from __future__ import annotations

from pathlib import Path

from ..models import McpServer, RemoteServer, StdioServer


def serialize_mcp(server: McpServer) -> dict:
    if isinstance(server, StdioServer):
        data: dict = {"command": server.command}
        if server.args:
            data["args"] = server.args
        if server.env:
            data["env"] = server.env
        return data

    assert isinstance(server, RemoteServer)
    return {"url": server.url}


def skill_dest_dirs(primary: Path) -> list[Path]:
    return [primary, Path.home() / ".agents" / "skills"]
