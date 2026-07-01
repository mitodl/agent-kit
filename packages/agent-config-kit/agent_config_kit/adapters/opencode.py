"""OpenCode adapter.

Verified against the real published schema (https://opencode.ai/config.json,
``$defs.McpLocalConfig``/``McpRemoteConfig``, fetched 2026-07-01 — see
``agent_config_kit/adapters/_wire/opencode_config.py``, vendored via D6
codegen). Three quirks that differ from the canonical model, confirming and
correcting what the design spec had flagged as "TBD":

- Requires a literal ``"type": "local"``/``"type": "remote"`` field (the spec
  draft's guess that OpenCode had no ``type`` field was wrong).
- Folds ``command``/``args`` into a single array (``McpLocalConfig.command``
  is ``list[str]``, not a separate command+args pair).
- The env var field is named ``environment``, not ``env``.
- ``timeout`` is milliseconds (int), not ``timeout_seconds`` (float).
"""

from __future__ import annotations

from ..models import McpServer, RemoteServer, StdioServer


def serialize_mcp(server: McpServer) -> dict:
    if isinstance(server, StdioServer):
        data: dict = {"type": "local", "command": [server.command, *server.args]}
        if server.cwd is not None:
            data["cwd"] = server.cwd
        if server.env:
            data["environment"] = server.env
    else:
        assert isinstance(server, RemoteServer)
        data = {"type": "remote", "url": server.url}
        if server.headers:
            data["headers"] = server.headers
        if server.oauth is not None:
            data["oauth"] = server.oauth

    if server.timeout_seconds is not None:
        data["timeout"] = round(server.timeout_seconds * 1000)

    return data
