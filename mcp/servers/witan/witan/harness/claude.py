"""Claude Code, run as ``claude -p --output-format stream-json --verbose``.

Flags verified against ``claude --help`` on Claude Code 2.1.332 (2026-11-25):
``--session-id <uuid>`` names a new session, ``--resume <id>`` continues one,
and stream-json needs ``--verbose`` in print mode. The prompt goes on stdin,
which ``-p`` reads when no prompt argument is given, so a prompt that starts
with ``-`` or exceeds the kernel's per-argument limit still arrives intact.

Permissions: the worker runs with ``--permission-mode`` (``acceptEdits`` by
default) in the worktree, with no ``--add-dir``, so the worktree is its only
working directory. ``--permission-prompts none`` makes anything that would
otherwise prompt fail instead of blocking a headless run, and
``SessionSpec.allowed_tools`` goes to ``--allowedTools`` for commands the
worker must run without asking (e.g. ``Bash(uv run pytest:*)``).

The user, project and local settings files still load, because they carry the
hooks witan's context injection runs through. Their ``permissions.allow``
rules apply to the worker too, so a rule that allows edits under a directory
of worktrees lets one worker edit a sibling's worktree without asking.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, get_args

from .acp import (
    AgentMessageChunk,
    AgentThoughtChunk,
    Cost,
    CurrentModeUpdate,
    Notice,
    Plan,
    PlanEntry,
    SessionUpdate,
    StopReason,
    ToolCall,
    ToolCallContent,
    ToolCallLocation,
    ToolCallUpdate,
    ToolKind,
    UsageUpdate,
    text,
)
from .base import SessionSpec
from .native import CliAdapter, absolute, clip, clip_raw

TOOL_KINDS: dict[str, ToolKind] = {
    "Read": "read",
    "Write": "edit",
    "Edit": "edit",
    "MultiEdit": "edit",
    "NotebookEdit": "edit",
    "Bash": "execute",
    "PowerShell": "execute",
    "Grep": "search",
    "Glob": "search",
    "ToolSearch": "search",
    "WebFetch": "fetch",
    "WebSearch": "fetch",
    "TodoWrite": "think",
    "EnterPlanMode": "switch_mode",
    "ExitPlanMode": "switch_mode",
}
# The input field that best says what a call is doing, for its title.
_TITLE_FIELDS = (
    "description",
    "command",
    "file_path",
    "notebook_path",
    "path",
    "pattern",
    "url",
    "query",
)
_PATH_FIELDS = ("file_path", "notebook_path", "path")
_STOP_REASONS = frozenset(get_args(StopReason))


def _tool_call(block: dict[str, Any], cwd: Path) -> list[SessionUpdate]:
    name = block["name"]
    tool_input = block.get("input") or {}
    summary = next(
        (
            str(tool_input[k])
            for k in _TITLE_FIELDS
            if isinstance(tool_input.get(k), str)
        ),
        None,
    )
    updates: list[SessionUpdate] = [
        ToolCall(
            tool_call_id=block["id"],
            title=clip(f"{name}: {summary}") if summary else name,
            name=name,
            kind=TOOL_KINDS.get(name, "other"),
            status="in_progress",
            locations=[
                ToolCallLocation(path=absolute(tool_input[k], cwd))
                for k in _PATH_FIELDS
                if isinstance(tool_input.get(k), str)
            ],
            raw_input=clip_raw(tool_input),
        )
    ]
    if name == "TodoWrite" and isinstance(tool_input.get("todos"), list):
        updates.append(
            Plan(
                entries=[
                    PlanEntry(
                        content=todo["content"],
                        priority="medium",
                        status=todo["status"],
                    )
                    for todo in tool_input["todos"]
                ]
            )
        )
    return updates


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


class ClaudeCodec:
    """stream-json lines from ``claude -p`` to ACP session updates."""

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self.session_id: str | None = None
        self.stop_reason: StopReason | None = None
        self.error: str | None = None
        self.usage: dict[str, Any] = {}
        self._context_used = 0

    def decode(self, record: dict[str, Any]) -> list[SessionUpdate]:
        match record.get("type"):
            case "system":
                return self._system(record)
            case "assistant":
                return self._assistant(record["message"])
            case "user":
                return self._user(record["message"])
            case "result":
                return self._result(record)
        # rate_limit_event, stream_event and anything newer: nothing a person
        # watching the worker needs.
        return []

    def _system(self, record: dict[str, Any]) -> list[SessionUpdate]:
        subtype = record.get("subtype")
        if subtype == "init":
            self.session_id = record["session_id"]
            return [CurrentModeUpdate(current_mode_id=record["permissionMode"])]
        if subtype == "hook_response" and (
            record.get("outcome") != "success" or record.get("exit_code")
        ):
            return [
                Notice(
                    severity="warning",
                    title=f"{record['hook_name']} hook failed",
                    description=clip(
                        record.get("stderr") or record.get("output") or ""
                    ),
                )
            ]
        return []

    def _assistant(self, message: dict[str, Any]) -> list[SessionUpdate]:
        usage = message.get("usage") or {}
        self._context_used = (
            usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("output_tokens", 0)
        ) or self._context_used
        updates: list[SessionUpdate] = []
        for block in message["content"]:
            match block.get("type"):
                case "text" if block["text"]:
                    updates.append(
                        AgentMessageChunk(
                            content=text(block["text"]), message_id=message.get("id")
                        )
                    )
                # Thinking arrives empty when the model's reasoning is not
                # displayed; there is nothing to show then.
                case "thinking" if block.get("thinking"):
                    updates.append(
                        AgentThoughtChunk(
                            content=text(block["thinking"]),
                            message_id=message.get("id"),
                        )
                    )
                case "tool_use":
                    updates.extend(_tool_call(block, self.cwd))
        return updates

    def _user(self, message: dict[str, Any]) -> list[SessionUpdate]:
        content = message.get("content")
        if not isinstance(content, list):
            return []
        return [
            ToolCallUpdate(
                tool_call_id=block["tool_use_id"],
                status="failed" if block.get("is_error") else "completed",
                content=[
                    ToolCallContent(
                        content=text(clip(_result_text(block.get("content"))))
                    )
                ],
            )
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]

    def _result(self, record: dict[str, Any]) -> list[SessionUpdate]:
        self.session_id = record.get("session_id") or self.session_id
        reason = record.get("stop_reason")
        if record.get("subtype") == "error_max_turns":
            reason = "max_turn_requests"
        self.stop_reason = reason if reason in _STOP_REASONS else None
        if record.get("is_error"):
            result = record.get("result")
            self.error = (
                result
                if isinstance(result, str) and result
                else record.get("subtype", "error")
            )
        usage = record.get("usage") or {}
        self.usage = {
            key: usage[key]
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
            if key in usage
        }
        cost = record.get("total_cost_usd")
        if cost is not None:
            self.usage["cost_usd"] = cost
        windows = [
            m["contextWindow"]
            for m in (record.get("modelUsage") or {}).values()
            if "contextWindow" in m
        ]
        if not windows:
            return []
        return [
            UsageUpdate(
                used=self._context_used,
                size=max(windows),
                cost=Cost(amount=cost, currency="USD") if cost is not None else None,
            )
        ]


class ClaudeAdapter(CliAdapter):
    """Claude Code in print mode, one prompt per process.

    :param permission_mode: Claude Code's ``--permission-mode``.
    """

    name: ClassVar[str] = "claude"
    default_executable: ClassVar[str] = "claude"
    # Exported by Claude Code 2.1.332 to the processes a session starts
    # (observed in such a session's environment). CLAUDECODE alone makes the
    # child behave as a nested session.
    parent_session_env: ClassVar[tuple[str, ...]] = (
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_CODE_SESSION_ATTENDED",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_CODE_MESSAGING_TOKEN",
        "CLAUDE_CODE_BRIDGE_SESSION_ID",
        "CLAUDE_CODE_CHILD_NOTES_DIR",
        "CLAUDE_PID",
        "CLAUDE_PROJECT_DIR",
    )

    def __init__(
        self, executable: str | None = None, *, permission_mode: str = "acceptEdits"
    ) -> None:
        super().__init__(executable)
        self.permission_mode = permission_mode

    def _argv(self, spec: SessionSpec, session_flag: str, session_id: str) -> list[str]:
        argv = [
            self.executable,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            session_flag,
            session_id,
            "--permission-mode",
            self.permission_mode,
            "--permission-prompts",
            "none",
        ]
        if spec.model:
            argv += ["--model", spec.model]
        # Variadic, so it goes last: nothing positional follows it.
        if spec.allowed_tools:
            argv += ["--allowedTools", *spec.allowed_tools]
        return argv

    def start_argv(self, spec: SessionSpec, session_id: str) -> list[str]:
        return self._argv(spec, "--session-id", session_id)

    def resume_argv(self, spec: SessionSpec, session_id: str) -> list[str]:
        return self._argv(spec, "--resume", session_id)

    def stdin_prompt(self, spec: SessionSpec) -> str | None:
        return spec.prompt

    def codec(self, cwd: Path) -> ClaudeCodec:
        return ClaudeCodec(cwd)
