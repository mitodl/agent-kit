"""The ACP v1 session updates the harness adapters emit, as pydantic models.

Agent Client Protocol (https://agentclientprotocol.com) session updates are the
event model every adapter normalizes its harness's output into, so one event
log format and one reader cover every harness, and an ACP adapter can pass an
agent's own ``session/update`` notifications straight through. These models
follow ``schema/v1/schema.json`` at agentclientprotocol/agent-client-protocol
7855babf8bee9db299ce668dfa911289f4d1cf4e, pinned to protocol version 1.

Only the update variants and content shapes a native adapter produces are
modelled. Unknown fields are kept (``extra="allow"``) so an update read back
from a log, or relayed from an ACP agent, survives a round trip. witan's own
data rides in ``_meta`` under the ``"witan"`` key, which the protocol reserves
for implementations.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

PROTOCOL_VERSION = 1
META_KEY = "witan"

ToolKind = Literal[
    "read",
    "edit",
    "delete",
    "move",
    "search",
    "execute",
    "think",
    "fetch",
    "switch_mode",
    "other",
]
ToolCallStatus = Literal["pending", "in_progress", "completed", "failed"]
StopReason = Literal[
    "end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"
]
PlanEntryStatus = Literal["pending", "in_progress", "completed"]
PlanEntryPriority = Literal["high", "medium", "low"]


class AcpModel(BaseModel):
    """camelCase on the wire, snake_case in Python, ``_meta`` as ``meta``."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="allow"
    )

    meta: dict[str, Any] | None = Field(default=None, alias="_meta")

    def wire(self) -> dict[str, Any]:
        """The update as ACP JSON: aliased keys, unset optionals left out."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class TextContent(AcpModel):
    type: Literal["text"] = "text"
    text: str


class ToolCallContent(AcpModel):
    """A tool call's ``content``-typed output (ACP's ``ToolCallContent``)."""

    type: Literal["content"] = "content"
    content: TextContent


class ToolCallLocation(AcpModel):
    path: str
    line: int | None = None


class UserMessageChunk(AcpModel):
    session_update: Literal["user_message_chunk"] = "user_message_chunk"
    content: TextContent
    message_id: str | None = None


class AgentMessageChunk(AcpModel):
    session_update: Literal["agent_message_chunk"] = "agent_message_chunk"
    content: TextContent
    message_id: str | None = None


class AgentThoughtChunk(AcpModel):
    session_update: Literal["agent_thought_chunk"] = "agent_thought_chunk"
    content: TextContent
    message_id: str | None = None


class ToolCall(AcpModel):
    session_update: Literal["tool_call"] = "tool_call"
    tool_call_id: str
    title: str
    name: str | None = None
    kind: ToolKind = "other"
    status: ToolCallStatus = "pending"
    content: list[ToolCallContent] = Field(default_factory=list)
    locations: list[ToolCallLocation] = Field(default_factory=list)
    raw_input: Any = None
    raw_output: Any = None


class ToolCallUpdate(AcpModel):
    session_update: Literal["tool_call_update"] = "tool_call_update"
    tool_call_id: str
    kind: ToolKind | None = None
    status: ToolCallStatus | None = None
    title: str | None = None
    name: str | None = None
    content: list[ToolCallContent] | None = None
    locations: list[ToolCallLocation] | None = None
    raw_input: Any = None
    raw_output: Any = None


class PlanEntry(AcpModel):
    content: str
    priority: PlanEntryPriority
    status: PlanEntryStatus


class Plan(AcpModel):
    session_update: Literal["plan"] = "plan"
    entries: list[PlanEntry]


class CurrentModeUpdate(AcpModel):
    session_update: Literal["current_mode_update"] = "current_mode_update"
    current_mode_id: str


class SessionInfoUpdate(AcpModel):
    session_update: Literal["session_info_update"] = "session_info_update"
    title: str | None = None
    updated_at: str | None = None


class Cost(AcpModel):
    amount: float
    currency: str


class UsageUpdate(AcpModel):
    session_update: Literal["usage_update"] = "usage_update"
    used: int
    size: int
    cost: Cost | None = None


class Notice(AcpModel):
    session_update: Literal["notice"] = "notice"
    severity: str
    """``info``, ``warning`` or ``error``; the schema leaves room for others."""
    title: str = Field(min_length=1)
    description: str | None = None


SessionUpdate = Annotated[
    UserMessageChunk
    | AgentMessageChunk
    | AgentThoughtChunk
    | ToolCall
    | ToolCallUpdate
    | Plan
    | CurrentModeUpdate
    | SessionInfoUpdate
    | UsageUpdate
    | Notice,
    Field(discriminator="session_update"),
]


class SessionNotification(AcpModel):
    """One ``session/update`` notification: a line of a session's event log."""

    session_id: str
    update: SessionUpdate


def text(value: str) -> TextContent:
    return TextContent(text=value)


def witan_meta(**fields: Any) -> dict[str, Any]:
    """A ``_meta`` value carrying witan's fields under its own key."""
    return {META_KEY: fields}
