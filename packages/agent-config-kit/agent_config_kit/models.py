"""Canonical, capability-cluster data models shared across coding-agent platforms.

Field-name and structural variance between agents (``env`` vs ``environment``,
``disabled`` vs ``enabled``, command-as-string vs command-as-array) is
adapter-layer concern — see ``agent_config_kit.adapters`` — and is never
modeled here.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, Union

from pydantic import BaseModel, Field

# ── Capability 1: MCP servers ────────────────────────────────────────────────


class ApprovalMode(str, Enum):
    ALWAYS_ALLOW = "always_allow"
    ASK = "ask"
    NEVER = "never"


class ApprovalPolicy(BaseModel):
    mode: ApprovalMode = ApprovalMode.ASK
    allowed_tools: list[str] | None = None


class StdioServer(BaseModel):
    kind: Literal["stdio"] = "stdio"
    command: str
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float | None = None
    approval: ApprovalPolicy = Field(default_factory=ApprovalPolicy)


class RemoteServer(BaseModel):
    kind: Literal["remote"] = "remote"
    url: str
    transport: Literal["sse", "http", "streamable-http"] = "streamable-http"
    headers: dict[str, str] = Field(default_factory=dict)
    oauth: dict[str, Any] | None = None
    timeout_seconds: float | None = None
    approval: ApprovalPolicy = Field(default_factory=ApprovalPolicy)


McpServer = Annotated[Union[StdioServer, RemoteServer], Field(discriminator="kind")]

# ── Capability 2a: plain-file instructions (AGENTS.md-style) ────────────────


class SearchStrategy(str, Enum):
    UPWARD_WALK = "upward_walk"
    FIRST_MATCH = "first_match"
    FIXED_DIRS = "fixed_dirs"


class InstructionsConfig(BaseModel):
    candidate_filenames: list[str]
    search_strategy: SearchStrategy
    read_by_default: bool = True


# ── Capability 2b: frontmatter-scoped rules (Continue/Cline-style) ──────────


class FrontmatterRule(BaseModel):
    name: str
    rule: str
    description: str | None = None
    globs: list[str] | None = None
    regex: list[str] | None = None
    always_apply: bool = False


# ── Capability 3: hooks ──────────────────────────────────────────────────────


class HookEvent(str, Enum):
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    STOP = "stop"


class DeclarativeHook(BaseModel):
    """Claude Code, Continue, Codex CLI, Gemini CLI, adapted Goose/Cline."""

    kind: Literal["declarative"] = "declarative"
    event: HookEvent
    matcher: str | None = None
    command: str
    timeout_seconds: float | None = None


class PluginRegistration(BaseModel):
    """OpenCode, Kilo Code, Pi — imperative callback files, not JSON."""

    kind: Literal["plugin"] = "plugin"
    entry_path: Path


Hook = Annotated[
    Union[DeclarativeHook, PluginRegistration], Field(discriminator="kind")
]

# ── Capability 4: LSP (modeled, no v1 populated entry/caller — spec D7) ─────


class LspServer(BaseModel):
    command: list[str]
    extensions: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    initialization_options: dict[str, Any] = Field(default_factory=dict)
    disabled: bool = False
    settings: dict[str, Any] | None = None
    fetch: dict[str, Any] | None = None


# ── Capability 5: skills (matches witan's existing SKILL.md convention) ────


class SkillSource(BaseModel):
    name: str
    skill_md_path: Path


# ── Cross-cutting: scope (global vs. project), spec D8 ──────────────────────


class Scope(str, Enum):
    GLOBAL = "global"
    PROJECT = "project"


class MergeStrategy(str, Enum):
    OVERRIDE_BY_KEY = "override_by_key"
    CONCATENATE = "concatenate"
    DEEP_MERGE = "deep_merge"  # e.g. Pi's settings.json


class ScopeTarget(BaseModel):
    path: Path
    key_path: tuple[str, ...] = ()  # e.g. ("mcpServers",) or ("hooks", "PreToolUse")


class CapabilityScope(BaseModel):
    global_: ScopeTarget | None = Field(default=None, alias="global")
    project: ScopeTarget | None = None
    merge_strategy: MergeStrategy = MergeStrategy.OVERRIDE_BY_KEY
    model_config = {"populate_by_name": True}


class AgentPlatform(BaseModel):
    name: str
    detect: Callable[[], bool] | None = None  # None => assumed always installed

    mcp: CapabilityScope | None = None
    mcp_conditional_on: str | None = (
        None  # e.g. Pi: "requires a third-party MCP plugin"
    )
    mcp_serialize: Callable[[McpServer], dict] | None = (
        None  # adapter wire-format projection
    )

    hooks: CapabilityScope | None = None
    hooks_merge: Callable[[dict, list[DeclarativeHook]], None] | None = None

    instructions: InstructionsConfig | None = None

    skills: CapabilityScope | None = None
    skill_dest_dirs: Callable[[Path], list[Path]] | None = (
        None  # e.g. Pi's dual dest dirs
    )

    lsp: CapabilityScope | None = None

    model_config = {"arbitrary_types_allowed": True}
