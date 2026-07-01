"""Validates each adapter's ``serialize_mcp``/``merge_hooks`` output against
the vendored, codegen'd wire-format models (``adapters/_wire/``), which were
generated from real published (or, where unpublished, hand-authored) JSON
Schemas per spec D6. Catches adapter/schema drift.
"""

from agent_config_kit.adapters import claude, copilot, opencode, pi
from agent_config_kit.adapters._wire.claude_settings import HookMatcher
from agent_config_kit.adapters._wire.copilot_mcp import CopilotMcpServer
from agent_config_kit.adapters._wire.opencode_config import (
    McpLocalConfig,
    McpRemoteConfig,
)
from agent_config_kit.adapters._wire.pi_mcp import PiMcpServer
from agent_config_kit.models import (
    DeclarativeHook,
    HookEvent,
    RemoteServer,
    StdioServer,
)

_STDIO = StdioServer(
    command="uvx", args=["witan", "serve"], env={"WITAN_AUTHOR": "tester"}
)


def test_claude_hook_merge_output_is_schema_valid():
    settings: dict = {}
    claude.merge_hooks(
        settings,
        [DeclarativeHook(event=HookEvent.STOP, command="witan session-checkpoint")],
    )
    HookMatcher.model_validate(settings["hooks"]["Stop"][0])


def test_copilot_serialized_mcp_entry_is_schema_valid():
    CopilotMcpServer.model_validate(copilot.serialize_mcp(_STDIO))


def test_pi_serialized_mcp_entry_is_schema_valid():
    PiMcpServer.model_validate(pi.serialize_mcp(_STDIO))


def test_opencode_serialized_stdio_entry_is_schema_valid():
    McpLocalConfig.model_validate(opencode.serialize_mcp(_STDIO))


def test_opencode_serialized_remote_entry_is_schema_valid():
    remote = RemoteServer(url="https://example.com/mcp")
    McpRemoteConfig.model_validate(opencode.serialize_mcp(remote))
