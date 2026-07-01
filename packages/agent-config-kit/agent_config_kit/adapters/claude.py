"""Claude Code adapter: requires a literal "type" field on MCP entries, and
merges declarative hooks into ~/.claude/settings.json's "hooks" key, deduped
by command per event (mirrors ``witan/setup.py``'s ``_merge_claude_hooks``).
"""

from __future__ import annotations

from ..models import DeclarativeHook, HookEvent, McpServer, RemoteServer, StdioServer

_EVENT_NAMES: dict[HookEvent, str] = {
    HookEvent.PRE_TOOL_USE: "PreToolUse",
    HookEvent.POST_TOOL_USE: "PostToolUse",
    HookEvent.SESSION_START: "SessionStart",
    HookEvent.SESSION_END: "SessionEnd",
    HookEvent.USER_PROMPT_SUBMIT: "UserPromptSubmit",
    HookEvent.STOP: "Stop",
}


def serialize_mcp(server: McpServer) -> dict:
    if isinstance(server, StdioServer):
        data: dict = {"type": "stdio", "command": server.command}
        if server.args:
            data["args"] = server.args
        if server.cwd is not None:
            data["cwd"] = server.cwd
        if server.env:
            data["env"] = server.env
    else:
        assert isinstance(server, RemoteServer)
        # Claude Code's remote MCP entries use "sse"/"http", not a generic
        # "remote" bucket (`claude mcp add --transport sse|http`). Not
        # confirmed against a published schema — ~/.claude.json (where MCP
        # servers actually live) has none — so this follows documented CLI
        # behavior rather than a fetched schema, unlike the OpenCode adapter.
        data = {
            "type": "http"
            if server.transport in ("http", "streamable-http")
            else "sse",
            "url": server.url,
        }
        if server.headers:
            data["headers"] = server.headers

    return data


def merge_hooks(settings: dict, hooks: list[DeclarativeHook]) -> None:
    if not isinstance(settings.get("hooks"), dict):
        settings["hooks"] = {}
    hooks_section = settings["hooks"]

    for hook in hooks:
        event_name = _EVENT_NAMES[hook.event]
        entry = {
            "matcher": hook.matcher or "",
            "hooks": [{"type": "command", "command": hook.command}],
        }
        if not isinstance(hooks_section.get(event_name), list):
            hooks_section[event_name] = []
        existing = hooks_section[event_name]
        already_present = any(
            isinstance(e, dict)
            and any(
                isinstance(h, dict) and h.get("command") == hook.command
                for h in e.get("hooks", [])
                if isinstance(e.get("hooks"), list)
            )
            for e in existing
        )
        if not already_present:
            existing.append(entry)
