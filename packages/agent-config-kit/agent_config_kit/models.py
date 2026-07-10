"""Canonical, capability-cluster data models shared across coding-agent platforms.

Field-name and structural variance between agents (``env`` vs ``environment``,
``disabled`` vs ``enabled``, command-as-string vs command-as-array) is
adapter-layer concern — see ``agent_config_kit.adapters`` — and is never
modeled here.
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


# ── Capability 5: skills (Agent Skills spec: https://agentskills.io/specification) ─

# Exported (not module-private) — installers.py and prune.py both re-check a
# skill name against this before using it to build a filesystem path, since
# that's the one place a crafted name could matter (path traversal): the
# field_validator below only protects names that go through SkillSource's own
# construction, not ones round-tripped through the prune state file's raw
# JSON strings.
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class SkillSource(BaseModel):
    """``skill_md_path``'s parent directory is treated as the skill's full
    root (Agent Skills packaging convention) — ``installers.install_skills``
    copies everything alongside ``SKILL.md`` (``scripts/``, ``references/``,
    ``assets/``, etc.), not just the file itself, since ``dest/name/`` is
    where the whole directory lands."""

    model_config = ConfigDict(validate_assignment=True)

    name: str
    skill_md_path: Path

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        # Mirrors the Agent Skills spec's SKILL.md `name` frontmatter field
        # constraints, since this `name` becomes that directory's name at
        # install time — an invalid name here would produce a
        # non-spec-compliant installed skill regardless of what the source
        # SKILL.md's own frontmatter says.
        if not 1 <= len(value) <= 64:
            raise ValueError(
                f"must be 1-64 characters (Agent Skills spec), got {len(value)}: {value!r}"
            )
        if not SKILL_NAME_PATTERN.fullmatch(value):
            raise ValueError(
                "must be lowercase alphanumeric segments separated by single "
                f"hyphens, with no leading/trailing/consecutive hyphens "
                f"(Agent Skills spec): {value!r}"
            )
        return value

    @field_validator("skill_md_path")
    @classmethod
    def _validate_skill_md_filename(cls, value: Path) -> Path:
        if value.name != "SKILL.md":
            raise ValueError(
                f"skill_md_path must point to a file literally named "
                f"'SKILL.md' (Agent Skills spec), got {value.name!r}"
            )
        return value


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
    model_config = ConfigDict(populate_by_name=True)


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
    hooks_remove: Callable[[dict, list[DeclarativeHook]], bool] | None = (
        None  # inverse of hooks_merge, for `apply --prune` (spec §5)
    )

    instructions: InstructionsConfig | None = None

    skills: CapabilityScope | None = None
    skill_dest_dirs: Callable[[Path], list[Path]] | None = (
        None  # e.g. OpenCode's dual dest dirs (singular/plural ambiguity)
    )

    lsp: CapabilityScope | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)
