"""Claude Code adapter: requires a literal "type" field on MCP entries, and
merges declarative hooks into ~/.claude/settings.json's "hooks" key, deduped
by command per event (mirrors ``witan/setup.py``'s ``_merge_claude_hooks``).
"""

from __future__ import annotations

from ..models import DeclarativeHook, HookEvent, McpServer

_EVENT_NAMES: dict[HookEvent, str] = {
    HookEvent.PRE_TOOL_USE: "PreToolUse",
    HookEvent.POST_TOOL_USE: "PostToolUse",
    HookEvent.SESSION_START: "SessionStart",
    HookEvent.SESSION_END: "SessionEnd",
    HookEvent.USER_PROMPT_SUBMIT: "UserPromptSubmit",
    HookEvent.STOP: "Stop",
}


def serialize_mcp(server: McpServer) -> dict:
    data = server.model_dump(
        mode="json", exclude={"kind", "approval"}, exclude_none=True
    )
    data["type"] = server.kind
    return data


def merge_hooks(settings: dict, hooks: list[DeclarativeHook]) -> None:
    for hook in hooks:
        event_name = _EVENT_NAMES[hook.event]
        entry = {
            "matcher": hook.matcher or "",
            "hooks": [{"type": "command", "command": hook.command}],
        }
        existing = settings.setdefault("hooks", {}).setdefault(event_name, [])
        already_present = any(
            any(h.get("command") == hook.command for h in e.get("hooks", []))
            for e in existing
        )
        if not already_present:
            existing.append(entry)
