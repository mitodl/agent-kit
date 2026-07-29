"""GitHub Copilot / VS Code adapter: requires a literal "type" field on
entries under <vscode-user-dir>/mcp.json's "servers" key. "type" is
"stdio"/"sse"/"http" — never a generic "remote" bucket — and remote-only
fields (headers/oauth) aren't part of the hand-authored wire model this was
verified against (see adapters/_wire/copilot_mcp.py; no upstream schema is
publicly fetchable for VS Code's mcp.json, per the design spec's open
questions), so they're intentionally dropped rather than guessed at.
"""

from __future__ import annotations

from ..models import McpServer, RemoteServer, StdioServer


def serialize_mcp(server: McpServer) -> dict:
    if isinstance(server, StdioServer):
        data: dict = {"type": "stdio", "command": server.command}
        if server.args:
            data["args"] = server.args
        if server.env:
            data["env"] = server.env
        return data

    assert isinstance(server, RemoteServer)
    # Only an explicit transport="sse" emits the type MCP 2026-07-28 deprecates;
    # anything else maps to "http" rather than falling back onto it.
    return {
        "type": "sse" if server.transport == "sse" else "http",
        "url": server.url,
    }
