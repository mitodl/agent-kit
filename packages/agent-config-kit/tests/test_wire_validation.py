"""Validates each adapter's ``serialize_mcp``/``merge_hooks`` output against
the vendored, codegen'd wire-format models (``adapters/_wire/``), which were
generated from real published (or, where unpublished, hand-authored) JSON
Schemas per spec D6. Catches adapter/schema drift.
"""

import pytest

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


def test_claude_hook_merge_serializes_and_refreshes_timeout():
    settings: dict = {}
    claude.merge_hooks(
        settings,
        [
            DeclarativeHook(
                event=HookEvent.USER_PROMPT_SUBMIT,
                command="witan inject-context",
                timeout_seconds=5,
            )
        ],
    )
    entry = settings["hooks"]["UserPromptSubmit"][0]
    HookMatcher.model_validate(entry)  # timeout must stay schema-valid
    assert entry["hooks"][0]["timeout"] == 5

    # Re-applying the same command must not duplicate it, and a changed timeout
    # is refreshed in place (so re-running setup applies it to existing installs).
    claude.merge_hooks(
        settings,
        [
            DeclarativeHook(
                event=HookEvent.USER_PROMPT_SUBMIT,
                command="witan inject-context",
                timeout_seconds=8,
            )
        ],
    )
    ups = settings["hooks"]["UserPromptSubmit"]
    assert len(ups) == 1
    assert ups[0]["hooks"][0]["timeout"] == 8


def test_copilot_serialized_mcp_entry_is_schema_valid():
    CopilotMcpServer.model_validate(copilot.serialize_mcp(_STDIO))


def test_pi_serialized_mcp_entry_is_schema_valid():
    PiMcpServer.model_validate(pi.serialize_mcp(_STDIO))


def test_opencode_serialized_stdio_entry_is_schema_valid():
    McpLocalConfig.model_validate(opencode.serialize_mcp(_STDIO))


def test_opencode_serialized_remote_entry_is_schema_valid():
    remote = RemoteServer(url="https://example.com/mcp")
    McpRemoteConfig.model_validate(opencode.serialize_mcp(remote))


def test_copilot_serialized_remote_entry_is_schema_valid_and_has_no_leaked_fields():
    # transport="sse" is deprecated but still supported for third-party servers
    # that only speak it — the warning is asserted in test_models.py.
    with pytest.deprecated_call():
        remote = RemoteServer(url="https://example.com/mcp", transport="sse")
    entry = copilot.serialize_mcp(remote)

    CopilotMcpServer.model_validate(entry)
    assert entry["type"] == "sse"
    assert "transport" not in entry
    assert "oauth" not in entry


def test_pi_serialized_remote_entry_is_schema_valid_and_has_no_leaked_fields():
    remote = RemoteServer(url="https://example.com/mcp")
    entry = pi.serialize_mcp(remote)

    PiMcpServer.model_validate(entry)
    assert entry == {"url": "https://example.com/mcp"}


def test_pi_serialized_remote_entry_with_oauth_transforms_callback_port():
    # Pi's real shape differs from the manifest's canonical
    # {clientId, callbackPort} — a top-level "auth" discriminator, and
    # callbackPort becomes a full localhost redirectUri.
    remote = RemoteServer(
        url="https://example.com/mcp",
        oauth={"clientId": "example-cli", "callbackPort": 8080},
    )
    entry = pi.serialize_mcp(remote)

    PiMcpServer.model_validate(entry)
    assert entry == {
        "url": "https://example.com/mcp",
        "auth": "oauth",
        "oauth": {
            "clientId": "example-cli",
            "redirectUri": "http://localhost:8080/callback",
        },
    }


def test_claude_serialized_remote_entry_has_no_leaked_fields():
    # No published schema covers ~/.claude.json's MCP servers (see spec's open
    # questions) so there's no vendored model to validate against — only
    # assert the adapter doesn't leak fields no real platform expects.
    remote = RemoteServer(url="https://example.com/mcp", transport="http")
    entry = claude.serialize_mcp(remote)

    assert entry == {"type": "http", "url": "https://example.com/mcp"}


def test_claude_serialized_remote_entry_with_oauth_is_passthrough():
    # Unlike Pi, Claude Code's own documented shape already matches the
    # manifest's canonical {clientId, callbackPort} — no transform needed.
    remote = RemoteServer(
        url="https://example.com/mcp",
        oauth={"clientId": "example-cli", "callbackPort": 8080},
    )
    entry = claude.serialize_mcp(remote)

    assert entry == {
        "type": "http",
        "url": "https://example.com/mcp",
        "oauth": {"clientId": "example-cli", "callbackPort": 8080},
    }
