import json
from pathlib import Path

from agent_config_kit.models import (
    DeclarativeHook,
    HookEvent,
    PluginRegistration,
    SkillSource,
    StdioServer,
)
from agent_config_kit.plan import RegistrationBundle, Scope, apply
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
    dest = tmp_path / ".pi" / "agent" / "skills" / "my-skill"
    assert dest.is_dir()
    previous = PlatformState(skills=["my-skill/SKILL.md"])

    result, current_state = apply_with_prune(
        "pi", _bundle(mcp_servers={}, skills=[]), previous
    )

    # every file gone -> the now-empty skill dir itself is cleaned up too
    assert not dest.exists()
    assert dest / "SKILL.md" in result.removed
    assert current_state.skills == []


def test_apply_with_prune_removes_dropped_skill_dirs_from_all_dest_dirs(
    tmp_path, monkeypatch
):
    """OpenCode writes each skill to both the singular and plural dir
    spelling (skill_dest_dirs) — prune must remove it from both."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.chdir(tmp_path)
    skill_md = tmp_path / "src" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("# skill")
    skill = SkillSource(name="my-skill", skill_md_path=skill_md)
    apply("opencode", _bundle(mcp_servers={}, skills=[skill]), scope=Scope.PROJECT)
    dest_singular = tmp_path / ".opencode" / "skill" / "my-skill"
    dest_plural = tmp_path / ".opencode" / "skills" / "my-skill"
    assert dest_singular.is_dir()
    assert dest_plural.is_dir()
    previous = PlatformState(skills=["my-skill/SKILL.md"])

    result, current_state = apply_with_prune(
        "opencode",
        _bundle(mcp_servers={}, skills=[]),
        previous,
        scope=Scope.PROJECT,
    )

    assert not dest_singular.exists()
    assert not dest_plural.exists()
    assert Path(".opencode") / "skill" / "my-skill" / "SKILL.md" in result.removed
    assert Path(".opencode") / "skills" / "my-skill" / "SKILL.md" in result.removed
    assert current_state.skills == []


def test_apply_with_prune_removes_stale_supporting_file_when_skill_name_unchanged(
    tmp_path, monkeypatch
):
    """The manifest only ever names a skill's SKILL.md, never its scripts/
    references/ files — those are discovered from disk at apply time. If a
    skill drops a supporting file between two applies but keeps the same
    name, prune must still catch and remove the now-stale file, not treat
    "same skill name in both manifests" as "nothing changed"."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "SKILL.md").write_text("# skill")
    (src_dir / "scripts").mkdir()
    old_script = src_dir / "scripts" / "old.sh"
    old_script.write_text("#!/bin/sh\necho old\n")
    skill = SkillSource(name="my-skill", skill_md_path=src_dir / "SKILL.md")
    _, previous = apply_with_prune(
        "claude", _bundle(mcp_servers={}, skills=[skill]), PlatformState()
    )
    dest_script = tmp_path / ".claude" / "skills" / "my-skill" / "scripts" / "old.sh"
    dest_skill_md = tmp_path / ".claude" / "skills" / "my-skill" / "SKILL.md"
    assert dest_script.exists()

    old_script.unlink()  # the source skill drops this script, name unchanged

    result, current_state = apply_with_prune(
        "claude", _bundle(mcp_servers={}, skills=[skill]), previous
    )

    assert not dest_script.exists()
    assert not dest_script.parent.exists()  # emptied scripts/ dir cleaned up
    assert dest_skill_md.exists()  # the skill itself is still installed
    assert dest_script in result.removed
    assert current_state.skills == ["my-skill/SKILL.md"]


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


def test_apply_with_prune_ignores_path_traversal_skill_entry(tmp_path, monkeypatch):
    """A crafted/corrupted state-file entry trying to escape dest_base via
    `..` must be skipped, not followed."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    canary = tmp_path / "canary.txt"
    canary.write_text("do not delete me")
    previous = PlatformState(skills=["my-skill/../../../canary.txt"])

    result, _ = apply_with_prune("claude", _bundle(mcp_servers={}, skills=[]), previous)

    assert canary.exists()
    assert result.removed == []


def test_apply_with_prune_ignores_invalid_skill_name_in_state_entry(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    dest = tmp_path / ".claude" / "skills" / "UPPERCASE" / "SKILL.md"
    dest.parent.mkdir(parents=True)
    dest.write_text("# skill")
    previous = PlatformState(skills=["UPPERCASE/SKILL.md"])

    result, _ = apply_with_prune("claude", _bundle(mcp_servers={}, skills=[]), previous)

    assert dest.exists()
    assert result.removed == []


def test_apply_with_prune_hook_removal_does_not_crash_on_directory(
    tmp_path, monkeypatch
):
    """If something other than agent-config-kit put a directory where a
    plugin hook file would be, removal must skip it (is_file, not exists)
    rather than crash with IsADirectoryError."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ext_dir = tmp_path / ".pi" / "agent" / "extensions" / "witan.ts"
    ext_dir.mkdir(parents=True)  # a directory, not a file, at the hook's path
    hook = PluginRegistration(entry_path=Path("/some/where/witan.ts"))
    previous = PlatformState(hooks=[hook_identity(hook)])

    result, _ = apply_with_prune("pi", _bundle(mcp_servers={}, hooks=[]), previous)

    assert ext_dir.is_dir()
    assert result.removed == []


def test_write_state_leaves_no_tmp_file_behind_after_success(tmp_path):
    manifest = tmp_path / "agent-config.toml"
    manifest.write_text("")
    state_path = default_state_path(manifest)

    write_state(state_path, manifest, {"claude": PlatformState(mcp_servers=["witan"])})

    assert state_path.exists()
    assert not state_path.with_name(state_path.name + ".tmp").exists()


def test_hook_identity_distinguishes_declarative_and_plugin():
    declarative = hook_identity(
        DeclarativeHook(event=HookEvent.STOP, command="witan session-checkpoint")
    )
    plugin = hook_identity(PluginRegistration(entry_path=Path("/x/witan.ts")))

    assert declarative == "declarative:stop:witan session-checkpoint"
    assert plugin == "plugin:witan.ts"
