"""Validates each adapter's ``serialize_mcp``/``merge_hooks`` output against
the vendored, codegen'd wire-format models (``adapters/_wire/``), which were
generated from real published (or, where unpublished, hand-authored) JSON
Schemas per spec D6. Catches adapter/schema drift.
"""

import pytest
from pydantic import ValidationError

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
    # Pi's own callbackPort means http://127.0.0.1:<port>/callback, but this
    # adapter has always written the manifest's callbackPort as the
    # localhost URI (a pre-registered client needs it exactly), so the port
    # becomes a callbackUrl. No "auth": "oauth":
    # Pi and pi-mcp-adapter 5.x both skip an entry whose auth is a string.
    remote = RemoteServer(
        url="https://example.com/mcp",
        oauth={"clientId": "example-cli", "callbackPort": 8080},
    )
    entry = pi.serialize_mcp(remote)

    PiMcpServer.model_validate(entry)
    assert entry == {
        "url": "https://example.com/mcp",
        "oauth": {
            "clientId": "example-cli",
            "callbackUrl": "http://localhost:8080/callback",
        },
    }


def test_pi_serialized_remote_entry_with_oauth_preserves_shared_fields():
    # clientSecret/scope/clientName/authServerMetadataUrl are identically
    # named on both the manifest's canonical shape and Pi's — they must pass
    # through untouched, not be dropped alongside the callbackPort rename.
    remote = RemoteServer(
        url="https://example.com/mcp",
        oauth={
            "clientId": "example-cli",
            "callbackPort": 8080,
            "clientSecret": "shh",
            "scope": "openid offline_access",
            "clientName": "Claude Code",
            "authServerMetadataUrl": "https://example.com/.well-known/oauth-authorization-server",
        },
    )
    entry = pi.serialize_mcp(remote)

    PiMcpServer.model_validate(entry)
    assert entry["oauth"] == {
        "clientId": "example-cli",
        "callbackUrl": "http://localhost:8080/callback",
        "clientSecret": "shh",
        "scope": "openid offline_access",
        "clientName": "Claude Code",
        "authServerMetadataUrl": "https://example.com/.well-known/oauth-authorization-server",
    }


def test_pi_serialized_remote_entry_with_oauth_explicit_redirect_uri_wins():
    # A manifest written for pi-mcp-adapter 2.x's redirectUri keeps its URI,
    # as Pi's callbackUrl, rather than one derived from callbackPort.
    remote = RemoteServer(
        url="https://example.com/mcp",
        oauth={
            "clientId": "example-cli",
            "callbackPort": 8080,
            "redirectUri": "http://localhost:3118/callback",
        },
    )
    entry = pi.serialize_mcp(remote)

    PiMcpServer.model_validate(entry)
    assert entry["oauth"] == {
        "clientId": "example-cli",
        "callbackUrl": "http://localhost:3118/callback",
    }


def test_pi_serialized_remote_entry_with_oauth_explicit_callback_url_wins():
    # Pi's own callbackUrl passes through, and so does a callbackPort beside
    # it: Pi adds that port to a callbackUrl that names none.
    remote = RemoteServer(
        url="https://example.com/mcp",
        oauth={
            "clientId": "example-cli",
            "callbackPort": 8080,
            "callbackUrl": "http://localhost/callback",
            "redirectUri": "http://localhost:3118/callback",
        },
    )
    entry = pi.serialize_mcp(remote)

    PiMcpServer.model_validate(entry)
    assert entry["oauth"] == {
        "clientId": "example-cli",
        "callbackPort": 8080,
        "callbackUrl": "http://localhost/callback",
    }


def test_pi_serialized_remote_entry_drops_callback_port_beside_a_ported_url():
    # Pi and pi-mcp-adapter 5.x both reject a callbackUrl whose port differs
    # from callbackPort, so a callbackUrl that names a port wins outright.
    remote = RemoteServer(
        url="https://example.com/mcp",
        oauth={
            "clientId": "example-cli",
            "callbackPort": 8080,
            "callbackUrl": "http://localhost:3118/callback",
        },
    )
    entry = pi.serialize_mcp(remote)

    PiMcpServer.model_validate(entry)
    assert entry["oauth"] == {
        "clientId": "example-cli",
        "callbackUrl": "http://localhost:3118/callback",
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


def test_pi_serialized_stdio_entry_with_cwd_is_schema_valid():
    server = StdioServer(command="uvx", args=["witan", "serve"], cwd="~/src/witan")
    entry = pi.serialize_mcp(server)

    PiMcpServer.model_validate(entry)
    assert entry == {
        "command": "uvx",
        "args": ["witan", "serve"],
        "cwd": "~/src/witan",
    }


def test_pi_serialized_remote_entry_with_headers_is_schema_valid():
    headers = {"Authorization": "Bearer ${API_TOKEN}", "X-Team": "mitodl"}
    remote = RemoteServer(url="https://example.com/mcp", headers=headers)
    entry = pi.serialize_mcp(remote)

    PiMcpServer.model_validate(entry)
    assert entry == {"url": "https://example.com/mcp", "headers": headers}


def test_pi_serialized_entries_omit_empty_and_default_fields():
    # Defaults (cwd=None, env={}, args=[], headers={}) must not surface as
    # null/empty keys: Pi only signs in with OAuth when an HTTP server sends
    # no Authorization header, so an empty headers object is noise at best.
    stdio = pi.serialize_mcp(StdioServer(command="uvx"))
    remote = pi.serialize_mcp(RemoteServer(url="https://example.com/mcp"))

    assert stdio == {"command": "uvx"}
    assert remote == {"url": "https://example.com/mcp"}


def test_pi_serialized_remote_entry_with_headers_and_oauth():
    remote = RemoteServer(
        url="https://example.com/mcp",
        headers={"X-Api-Key": "k"},
        oauth={"clientId": "example-cli", "callbackPort": 8080},
    )
    entry = pi.serialize_mcp(remote)

    PiMcpServer.model_validate(entry)
    assert entry == {
        "url": "https://example.com/mcp",
        "headers": {"X-Api-Key": "k"},
        "oauth": {
            "clientId": "example-cli",
            "callbackUrl": "http://localhost:8080/callback",
        },
    }


def test_pi_wire_model_rejects_wrongly_typed_cwd_and_headers():
    with pytest.raises(ValidationError):
        PiMcpServer.model_validate({"command": "uvx", "cwd": ["not", "a", "str"]})
    with pytest.raises(ValidationError):
        PiMcpServer.model_validate({"url": "u", "headers": {"X": 1}})
    with pytest.raises(ValidationError):
        PiMcpServer.model_validate({"url": "u", "headers": ["X: 1"]})


def test_pi_wire_model_rejects_adapter_only_fields():
    """Pi's validator rejects a string ``auth`` (it means
    ``{"provider": ...}``), and the adapter-2.x-only keys are not Pi's, so
    the model must not let a serializer drift back to either."""
    with pytest.raises(ValidationError):
        PiMcpServer.model_validate({"url": "https://example.com/mcp", "auth": "oauth"})
    for field in ("lifecycle", "directTools", "idleTimeout"):
        with pytest.raises(ValidationError):
            PiMcpServer.model_validate({"command": "uvx", field: "lazy"})
    with pytest.raises(ValidationError):
        PiMcpServer.model_validate(
            {"url": "https://example.com/mcp", "oauth": {"redirectUri": "u"}}
        )
    PiMcpServer.model_validate(
        {"url": "https://example.com/mcp", "auth": {"provider": "radius"}}
    )
