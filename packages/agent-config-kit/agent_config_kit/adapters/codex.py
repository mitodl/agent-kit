"""Codex CLI adapter: ``[mcp_servers.<name>]`` tables in ``config.toml``.

Field names follow Codex's MCP docs (learn.chatgpt.com/docs/extend/mcp,
fetched 2026-10-15) and what ``codex mcp add`` itself writes (codex-cli
0.162.1). Differences from the canonical model:

- No ``type`` field: a ``url`` makes an entry Streamable HTTP, a ``command``
  makes it stdio. Codex has no SSE transport, so ``transport="sse"`` still
  maps to ``url`` and will only work against a server that also speaks
  Streamable HTTP.
- Remote ``headers`` are ``http_headers``.
- ``oauth`` keys are snake_case (``client_id``, ``callback_port``), where the
  manifest's canonical shape is Claude Code's camelCase
  (``clientId``, ``callbackPort``).
- ``callback_port`` alone only sets the listener port: Codex still sends a
  redirect URI with a random path (``http://127.0.0.1:8080/callback/<id>``),
  which a client registered with a fixed loopback redirect rejects. So a
  ``callbackPort`` without a ``callbackUrl`` also gets a fixed
  ``http://localhost:<port>/callback``.

Declarative hooks go to ``$CODEX_HOME/hooks.json``, which has Claude Code's
``{"hooks": {"<Event>": [{matcher, hooks: [...]}]}}`` shape (codex-rs
``HooksFile``/``MatcherGroup``), so the Claude adapter's merge and dedup are
reused. Codex parses ``timeout`` as a whole number of seconds and drops the
whole file on a float, so timeouts are rounded up. Codex keeps hook trust
(``trusted_hash``) in its own config and runs a hook only once the user has
trusted it there; nothing here writes that state.
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path

from ..models import DeclarativeHook, McpServer, RemoteServer, StdioServer
from . import claude as claude_adapter

HOOKS_APPROVAL = "Codex won't run it until you trust it with /hooks in the Codex TUI"


def codex_home() -> Path:
    override = os.environ.get("CODEX_HOME")
    return Path(override) if override else Path.home() / ".codex"


def _snake_case(key: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()


def serialize_mcp(server: McpServer) -> dict:
    if isinstance(server, StdioServer):
        data: dict = {"command": server.command}
        if server.args:
            data["args"] = server.args
        if server.cwd is not None:
            data["cwd"] = server.cwd
        if server.env:
            data["env"] = server.env
        return data

    assert isinstance(server, RemoteServer)
    data = {"url": server.url}
    if server.headers:
        data["http_headers"] = server.headers
    if server.oauth is not None:
        oauth = {_snake_case(k): v for k, v in server.oauth.items()}
        if "callback_port" in oauth and "callback_url" not in oauth:
            oauth["callback_url"] = (
                f"http://localhost:{oauth['callback_port']}/callback"
            )
        data["oauth"] = oauth
    return data


def _whole_seconds(hook: DeclarativeHook) -> DeclarativeHook:
    if hook.timeout_seconds is None:
        return hook
    return hook.model_copy(update={"timeout_seconds": math.ceil(hook.timeout_seconds)})


def merge_hooks(hooks_file: dict, hooks: list[DeclarativeHook]) -> None:
    claude_adapter.merge_hooks(hooks_file, [_whole_seconds(h) for h in hooks])
