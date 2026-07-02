import json
from pathlib import Path

from agent_config_kit.models import (
    DeclarativeHook,
    HookEvent,
    PluginRegistration,
    SkillSource,
    StdioServer,
)
from agent_config_kit.plan import RegistrationBundle, apply
from agent_config_kit.prune import (
    PlatformState,
    apply_with_prune,
    default_state_path,
    hook_identity,
    load_state,
    write_state,
)


def _bundle(**overrides) -> RegistrationBundle:
    defaults = dict(
        mcp_servers={"witan": StdioServer(command="uvx", args=["witan", "serve"])},
    )
    defaults.update(overrides)
    return RegistrationBundle(**defaults)


def test_apply_with_prune_no_prior_state_removes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    bundle = _bundle()
    apply("claude", bundle)
    other = {"other": {"command": "foo"}}
    claude_json = tmp_path / ".claude.json"
    cfg = json.loads(claude_json.read_text())
    cfg["mcpServers"].update(other)
    claude_json.write_text(json.dumps(cfg))

    result, current_state = apply_with_prune("claude", bundle, PlatformState())

    assert result.removed == []
    cfg = json.loads(claude_json.read_text())
    assert "other" in cfg["mcpServers"]
    assert current_state.mcp_servers == ["witan"]


def test_apply_with_prune_removes_mcp_server_dropped_from_manifest(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    apply("claude", _bundle())
    claude_json = tmp_path / ".claude.json"
    cfg = json.loads(claude_json.read_text())
    cfg["mcpServers"]["untouched"] = {"command": "foo"}
    claude_json.write_text(json.dumps(cfg))
    previous = PlatformState(mcp_servers=["witan"])

    result, current_state = apply_with_prune(
        "claude", _bundle(mcp_servers={}), previous
    )

    cfg = json.loads(claude_json.read_text())
    assert "witan" not in cfg["mcpServers"]
    assert cfg["mcpServers"]["untouched"] == {"command": "foo"}
    assert "mcp_servers:witan" not in result.removed  # removed items aren't prefixed
    assert "witan" in result.removed
    assert current_state.mcp_servers == []


def test_apply_with_prune_dry_run_reports_but_does_not_remove(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    apply("claude", _bundle())
    previous = PlatformState(mcp_servers=["witan"])

    result, _ = apply_with_prune(
        "claude", _bundle(mcp_servers={}), previous, dry_run=True
    )

    assert "witan" in result.removed
    cfg = json.loads((tmp_path / ".claude.json").read_text())
    assert "witan" in cfg["mcpServers"]


def test_apply_with_prune_removes_dropped_declarative_hook(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    hook = DeclarativeHook(event=HookEvent.STOP, command="witan session-checkpoint")
    other_hook = DeclarativeHook(
        event=HookEvent.USER_PROMPT_SUBMIT, command="witan inject-context"
    )
    apply("claude", _bundle(mcp_servers={}, hooks=[hook, other_hook]))
    previous = PlatformState(hooks=[hook_identity(hook), hook_identity(other_hook)])

    result, current_state = apply_with_prune(
        "claude", _bundle(mcp_servers={}, hooks=[other_hook]), previous
    )

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    stop_hooks = settings["hooks"].get("Stop", [])
    assert stop_hooks == []
    prompt_hooks = settings["hooks"]["UserPromptSubmit"]
    assert any(
        h["command"] == "witan inject-context" for e in prompt_hooks for h in e["hooks"]
    )
    assert hook_identity(hook) in result.removed
    assert current_state.hooks == [hook_identity(other_hook)]


def test_apply_with_prune_removes_dropped_plugin_hook_file(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    plugin_src = tmp_path / "ext" / "witan.ts"
    plugin_src.parent.mkdir(parents=True)
    plugin_src.write_text("// stub")
    hook = PluginRegistration(entry_path=plugin_src)
    apply("pi", _bundle(mcp_servers={}, hooks=[hook]))
    dest = tmp_path / ".pi" / "agent" / "extensions" / "witan.ts"
    assert dest.exists()
    previous = PlatformState(hooks=[hook_identity(hook)])

    result, current_state = apply_with_prune(
        "pi", _bundle(mcp_servers={}, hooks=[]), previous
    )

    assert not dest.exists()
    assert hook_identity(hook) in result.removed
    assert current_state.hooks == []


def test_apply_with_prune_removes_dropped_skill_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    skill_md = tmp_path / "src" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("# skill")
    skill = SkillSource(name="my-skill", skill_md_path=skill_md)
    apply("pi", _bundle(mcp_servers={}, skills=[skill]))
    dest_claude_style = tmp_path / ".pi" / "agent" / "skills" / "my-skill"
    dest_agents = tmp_path / ".agents" / "skills" / "my-skill"
    assert dest_claude_style.is_dir()
    assert dest_agents.is_dir()
    previous = PlatformState(skills=["my-skill"])

    result, current_state = apply_with_prune(
        "pi", _bundle(mcp_servers={}, skills=[]), previous
    )

    assert not dest_claude_style.exists()
    assert not dest_agents.exists()
    assert dest_claude_style in result.removed
    assert dest_agents in result.removed
    assert current_state.skills == []


def test_apply_with_prune_never_touches_keys_manifest_never_owned(
    tmp_path, monkeypatch
):
    """A key a human hand-added directly must survive even if it's absent
    from both the previous and current manifest state."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(json.dumps({"mcpServers": {"hand-added": {"command": "x"}}}))
    previous = PlatformState(mcp_servers=["long-gone"])

    apply_with_prune("claude", _bundle(mcp_servers={}), previous)

    cfg = json.loads(claude_json.read_text())
    assert cfg["mcpServers"]["hand-added"] == {"command": "x"}


def test_load_state_missing_file_returns_empty(tmp_path):
    assert load_state(tmp_path / "does-not-exist.lock.json") == {}


def test_write_state_then_load_state_round_trips(tmp_path):
    manifest = tmp_path / "agent-config.toml"
    manifest.write_text("")
    state_path = default_state_path(manifest)

    write_state(
        state_path,
        manifest,
        {"claude": PlatformState(mcp_servers=["witan"], hooks=["h1"], skills=["s1"])},
    )
    loaded = load_state(state_path)

    assert loaded["claude"] == PlatformState(
        mcp_servers=["witan"], hooks=["h1"], skills=["s1"]
    )


def test_write_state_preserves_platforms_not_touched_this_run(tmp_path):
    """A single-platform prune run (e.g. --platform claude) must not erase a
    different platform's previously recorded state (spec §5 step 5)."""
    manifest = tmp_path / "agent-config.toml"
    manifest.write_text("")
    state_path = default_state_path(manifest)
    write_state(state_path, manifest, {"pi": PlatformState(mcp_servers=["witan"])})

    states = load_state(state_path)
    states["claude"] = PlatformState(mcp_servers=["witan"])
    write_state(state_path, manifest, states)

    loaded = load_state(state_path)
    assert set(loaded) == {"pi", "claude"}


def test_default_state_path_is_manifest_name_plus_lock_json(tmp_path):
    manifest = tmp_path / "agent-config.toml"
    assert default_state_path(manifest) == tmp_path / "agent-config.toml.lock.json"


def test_hook_identity_distinguishes_declarative_and_plugin():
    declarative = hook_identity(
        DeclarativeHook(event=HookEvent.STOP, command="witan session-checkpoint")
    )
    plugin = hook_identity(PluginRegistration(entry_path=Path("/x/witan.ts")))

    assert declarative == "declarative:stop:witan session-checkpoint"
    assert plugin == "plugin:witan.ts"
