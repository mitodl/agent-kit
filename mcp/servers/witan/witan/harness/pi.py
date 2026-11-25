"""Pi, run as ``pi -p --mode json``.

Flags verified against ``pi --help`` on pi 1.5.2 (2026-11-25):
``--session-id <id>`` uses the current directory's session with that id,
creating it if there is none, and ``--session <id>`` opens an existing one and
exits 1 when there is none. Start uses the first and resume the second, so a
resume in the wrong directory fails instead of silently starting over.
The prompt goes on stdin, which Pi reads as the message in print mode
(``readPipedStdin`` in pi's ``dist/main.js``): in argv, a prompt starting with
``@`` would be taken as a file to attach even after ``--``, and one over the
kernel's per-argument limit would not launch at all. Pi trims the message.

Permissions: Pi asks no permission questions, so nothing can block a headless
run, and nothing confines its ``bash`` tool to the worktree either; the
worktree is only its working directory. ``SessionSpec.allowed_tools``, when
given, becomes Pi's ``--tools`` allowlist (e.g. ``read,edit,write`` for a
worker that must not run commands).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from .acp import (
    AgentMessageChunk,
    AgentThoughtChunk,
    Notice,
    SessionUpdate,
    StopReason,
    ToolCall,
    ToolCallContent,
    ToolCallLocation,
    ToolCallUpdate,
    ToolKind,
    text,
)
from .base import SessionSpec
from .native import CliAdapter, absolute, clip, clip_raw

TOOL_KINDS: dict[str, ToolKind] = {
    "read": "read",
    "write": "edit",
    "edit": "edit",
    "bash": "execute",
    "powershell": "execute",
    "grep": "search",
    "find": "search",
    "ls": "search",
}
# Pi's AssistantMessage.stopReason values that end a turn, in ACP's terms.
# "error" is reported through ``error`` instead, and "toolUse" never ends one.
STOP_REASONS: dict[str, StopReason] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "aborted": "cancelled",
}
_USAGE_FIELDS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cacheRead": "cache_read_input_tokens",
    "cacheWrite": "cache_creation_input_tokens",
}


def _content_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


class PiCodec:
    """``pi --mode json`` lines to ACP session updates."""

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self.session_id: str | None = None
        self.stop_reason: StopReason | None = None
        self.error: str | None = None
        self.usage: dict[str, Any] = {}

    def decode(self, record: dict[str, Any]) -> list[SessionUpdate]:
        match record.get("type"):
            case "session":
                self.session_id = record["id"]
            case "message_update":
                return self._delta(record["assistantMessageEvent"])
            case "message_end" if record["message"].get("role") == "assistant":
                return self._assistant_end(record["message"])
            case "tool_execution_start":
                args = record.get("args") or {}
                path = args.get("path")
                return [
                    ToolCall(
                        tool_call_id=record["toolCallId"],
                        title=clip(f"{record['toolName']}: {path}")
                        if isinstance(path, str)
                        else record["toolName"],
                        name=record["toolName"],
                        kind=TOOL_KINDS.get(record["toolName"], "other"),
                        status="in_progress",
                        locations=[ToolCallLocation(path=absolute(path, self.cwd))]
                        if isinstance(path, str)
                        else [],
                        raw_input=clip_raw(args),
                    )
                ]
            case "tool_execution_end":
                result = record.get("result") or {}
                return [
                    ToolCallUpdate(
                        tool_call_id=record["toolCallId"],
                        status="failed" if record.get("isError") else "completed",
                        content=[
                            ToolCallContent(
                                content=text(clip(_content_text(result.get("content"))))
                            )
                        ],
                    )
                ]
            case "auto_retry_start":
                return [
                    Notice(
                        severity="warning",
                        title=f"retrying the model call ({record['attempt']}/{record['maxAttempts']})",
                        description=record.get("errorMessage"),
                    )
                ]
        # The user's own message is logged by the session, and the system
        # prompt, turn and agent markers carry nothing a person watching needs.
        return []

    def _delta(self, event: dict[str, Any]) -> list[SessionUpdate]:
        match event.get("type"):
            case "text_delta" if event["delta"]:
                return [AgentMessageChunk(content=text(event["delta"]))]
            case "thinking_delta" if event["delta"]:
                return [AgentThoughtChunk(content=text(event["delta"]))]
        return []

    def _assistant_end(self, message: dict[str, Any]) -> list[SessionUpdate]:
        usage = message.get("usage") or {}
        for pi_key, key in _USAGE_FIELDS.items():
            self.usage[key] = self.usage.get(key, 0) + usage.get(pi_key, 0)
        cost = (usage.get("cost") or {}).get("total")
        if cost:
            self.usage["cost_usd"] = self.usage.get("cost_usd", 0) + cost
        reason = message.get("stopReason")
        if reason == "toolUse":
            return []
        self.stop_reason = STOP_REASONS.get(reason)
        # The last assistant message decides: an auto-retry that succeeds
        # clears an earlier failure.
        self.error = (
            (message.get("errorMessage") or "error").strip()
            if reason == "error"
            else None
        )
        if self.error is None:
            return []
        return [
            Notice(severity="error", title="model call failed", description=self.error)
        ]


class PiAdapter(CliAdapter):
    """Pi in print mode, one prompt per process."""

    name: ClassVar[str] = "pi"
    default_executable: ClassVar[str] = "pi"

    def _argv(self, spec: SessionSpec, session_flag: str, session_id: str) -> list[str]:
        argv = [self.executable, "-p", "--mode", "json", session_flag, session_id]
        if spec.model:
            argv += ["--model", spec.model]
        if spec.allowed_tools:
            argv += ["--tools", ",".join(spec.allowed_tools)]
        return argv

    def stdin_prompt(self, spec: SessionSpec) -> str | None:
        return spec.prompt

    def start_argv(self, spec: SessionSpec, session_id: str) -> list[str]:
        return self._argv(spec, "--session-id", session_id)

    def resume_argv(self, spec: SessionSpec, session_id: str) -> list[str]:
        return self._argv(spec, "--session", session_id)

    def codec(self, cwd: Path) -> PiCodec:
        return PiCodec(cwd)
