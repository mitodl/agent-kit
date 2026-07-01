from pathlib import Path

from pydantic import TypeAdapter

from agent_config_kit.models import (
    CapabilityScope,
    DeclarativeHook,
    Hook,
    HookEvent,
    McpServer,
    PluginRegistration,
    RemoteServer,
    ScopeTarget,
    StdioServer,
)

_MCP_ADAPTER: TypeAdapter = TypeAdapter(McpServer)
_HOOK_ADAPTER: TypeAdapter = TypeAdapter(Hook)


def test_stdio_server_round_trips_through_discriminated_union():
    server = StdioServer(
        command="uvx", args=["witan", "serve"], env={"WITAN_AUTHOR": "tester"}
    )
    dumped = _MCP_ADAPTER.dump_python(server, mode="json")
    restored = _MCP_ADAPTER.validate_python(dumped)
    assert isinstance(restored, StdioServer)
    assert restored == server


def test_remote_server_round_trips_through_discriminated_union():
    server = RemoteServer(url="https://example.com/mcp")
    dumped = _MCP_ADAPTER.dump_python(server, mode="json")
    restored = _MCP_ADAPTER.validate_python(dumped)
    assert isinstance(restored, RemoteServer)
    assert restored.transport == "streamable-http"


def test_hook_union_discriminates_declarative_vs_plugin():
    declarative = DeclarativeHook(
        event=HookEvent.STOP, command="witan session-checkpoint"
    )
    plugin = PluginRegistration(entry_path=Path("/tmp/hook.ts"))

    assert (
        _HOOK_ADAPTER.validate_python(
            _HOOK_ADAPTER.dump_python(declarative, mode="json")
        )
        == declarative
    )
    assert (
        _HOOK_ADAPTER.validate_python(_HOOK_ADAPTER.dump_python(plugin, mode="json"))
        == plugin
    )


def test_capability_scope_accepts_global_via_alias_or_field_name():
    by_alias = CapabilityScope(**{"global": ScopeTarget(path=Path("/x"))})
    by_name = CapabilityScope(global_=ScopeTarget(path=Path("/x")))
    assert by_alias.global_ == by_name.global_


def test_capability_scope_dump_by_alias_uses_global_key():
    """Known gotcha (spec §4): model_dump_json() drops the alias unless by_alias=True."""
    scope = CapabilityScope(global_=ScopeTarget(path=Path("/x")))

    assert "global_" in scope.model_dump(mode="json")
    dumped = scope.model_dump(mode="json", by_alias=True)
    assert "global" in dumped
    assert "global_" not in dumped
