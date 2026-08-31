"""Pi adapter: no "type" field on MCP entries — stdio and remote servers share
one shape distinguished by presence of "command" vs "url" (see
adapters/_wire/pi_mcp.py).

Skills install only to Pi's own skills dir (~/.pi/agent/skills/), not also to
the shared cross-agent pool (~/.agents/skills/): Pi already natively unions
both directories when discovering skills (its own docs/skills.md "Locations"
section), so writing the same skill into both is pure duplication — Pi finds
the name twice and logs a spurious "skill collision" warning on every
startup. A single dest dir is correct and sufficient.
"""

from __future__ import annotations

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
    data = {"url": server.url}
    # The manifest's canonical oauth shape is {clientId, callbackPort},
    # matching Claude Code's own documented shape (see claude.py) — but Pi's
    # real shape differs: a top-level "auth": "oauth" discriminator, and an
    # "oauth" object keyed by "redirectUri" (a full localhost callback URL),
    # not "callbackPort" (an int). This is a real field-name transform, not a
    # passthrough. Verified against pi.dev/packages/pi-mcp-adapter (live docs
    # fetched 2026-08-31) — not against ``_wire/pi_mcp.py``, which predates
    # Pi's OAuth support and was hand-authored (no published schema exists
    # for Pi to codegen from, per spec D6), so it was hand-updated alongside
    # this adapter rather than regenerated. Re-verify both together if Pi's
    # OAuth config shape changes.
    if server.oauth is not None:
        data["auth"] = "oauth"
        oauth: dict = {}
        if "clientId" in server.oauth:
            oauth["clientId"] = server.oauth["clientId"]
        if "callbackPort" in server.oauth:
            oauth["redirectUri"] = (
                f"http://localhost:{server.oauth['callbackPort']}/callback"
            )
        data["oauth"] = oauth
    return data
