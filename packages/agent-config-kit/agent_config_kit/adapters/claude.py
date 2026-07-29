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
        #
        # Only an explicit transport="sse" emits the deprecated SSE type; any
        # future transport falls through to "http" rather than being silently
        # downgraded onto a transport MCP 2026-07-28 is retiring.
        data = {
            "type": "sse" if server.transport == "sse" else "http",
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
        command_hook: dict = {"type": "command", "command": hook.command}
        # Claude Code's per-command hook object accepts an optional integer
        # "timeout" (seconds); only emit it when set so hooks without one keep
        # their current shape.
        if hook.timeout_seconds is not None:
            command_hook["timeout"] = hook.timeout_seconds
        entry = {"matcher": hook.matcher or "", "hooks": [command_hook]}
        if not isinstance(hooks_section.get(event_name), list):
            hooks_section[event_name] = []
        existing = hooks_section[event_name]
        # Dedup on command. If the command is already registered, refresh its
        # timeout in place (so re-running setup applies a newly-added timeout to
        # an existing install); otherwise append the new entry.
        found = False
        for e in existing:
            if not isinstance(e, dict) or not isinstance(e.get("hooks"), list):
                continue
            for h in e["hooks"]:
                if isinstance(h, dict) and h.get("command") == hook.command:
                    found = True
                    if hook.timeout_seconds is not None:
                        h["timeout"] = hook.timeout_seconds
        if not found:
            existing.append(entry)


def remove_hooks(settings: dict, hooks: list[DeclarativeHook]) -> bool:
    """Inverse of ``merge_hooks``: remove entries matching the given hooks'
    (event, command) identity. Drops a matcher entry entirely once it has no
    remaining hooks, rather than leaving an empty ``"hooks": []`` behind."""
    hooks_section = settings.get("hooks")
    if not isinstance(hooks_section, dict):
        return False
    changed = False
    for hook in hooks:
        event_name = _EVENT_NAMES[hook.event]
        entries = hooks_section.get(event_name)
        if not isinstance(entries, list):
            continue
        kept = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                kept.append(entry)
                continue
            remaining = [
                h
                for h in entry["hooks"]
                if not (isinstance(h, dict) and h.get("command") == hook.command)
            ]
            if len(remaining) != len(entry["hooks"]):
                changed = True
            if remaining:
                kept.append({**entry, "hooks": remaining})
        hooks_section[event_name] = kept
    return changed
