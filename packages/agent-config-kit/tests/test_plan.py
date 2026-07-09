import json
from pathlib import Path

from agent_config_kit.models import (
    DeclarativeHook,
    HookEvent,
    MergeStrategy,
    PluginRegistration,
    Scope,
    SkillSource,
    StdioServer,
)
from agent_config_kit.paths import vscode_user_dir
from agent_config_kit.plan import (
    RegistrationBundle,
    _merge_into,
    _navigate,
    apply,
    apply_all,
)


def _bundle(**overrides) -> RegistrationBundle:
    defaults = dict(
        mcp_servers={
            "witan": StdioServer(
                command="uvx", args=["witan", "serve"], env={"WITAN_AUTHOR": "tester"}
            )
        },
    )
    defaults.update(overrides)
    return RegistrationBundle(**defaults)


def test_apply_claude_registers_mcp_server_as_stdio(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    apply("claude", _bundle())

    entry = json.loads((tmp_path / ".claude.json").read_text())["mcpServers"]["witan"]
    assert entry["type"] == "stdio"
    assert entry["command"] == "uvx"
    assert entry["env"]["WITAN_AUTHOR"] == "tester"


def test_apply_claude_preserves_existing_claude_json(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        json.dumps({"numStartups": 7, "mcpServers": {"other": {"command": "foo"}}})
    )

    apply("claude", _bundle())

    cfg = json.loads(claude_json.read_text())
    assert cfg["numStartups"] == 7
    assert cfg["mcpServers"]["other"] == {"command": "foo"}
    assert "witan" in cfg["mcpServers"]


def test_apply_claude_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    result = apply("claude", _bundle(), dry_run=True)

    assert not (tmp_path / ".claude.json").exists()
    assert result.written == []
    assert result.planned


def test_apply_claude_skips_non_object_claude_json(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text("[1, 2, 3]")

    result = apply("claude", _bundle())

    assert json.loads(claude_json.read_text()) == [1, 2, 3]
    assert result.skipped


def test_apply_claude_merges_declarative_hooks_deduped_by_command(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    bundle = _bundle(
        hooks=[
            DeclarativeHook(
                event=HookEvent.USER_PROMPT_SUBMIT, command="witan inject-context"
            ),
            DeclarativeHook(event=HookEvent.STOP, command="witan session-checkpoint"),
        ]
    )

    apply("claude", bundle)
    apply("claude", bundle)  # re-applying must not duplicate entries

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "mcpServers" not in settings
    prompt_hooks = settings["hooks"]["UserPromptSubmit"]
    assert (
        sum(
            1
            for e in prompt_hooks
            for h in e["hooks"]
            if h["command"] == "witan inject-context"
        )
        == 1
    )


def test_apply_claude_tolerates_non_object_mcp_servers_key(tmp_path, monkeypatch):
    """A hand-edited .claude.json with "mcpServers": [] (wrong shape) must not
    crash apply() — the malformed value is coerced to an object rather than
    raising AttributeError from dict.setdefault on a list."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(json.dumps({"mcpServers": []}))

    apply("claude", _bundle())

    cfg = json.loads(claude_json.read_text())
    assert cfg["mcpServers"]["witan"]["command"] == "uvx"


def test_apply_claude_tolerates_non_object_hooks_value(tmp_path, monkeypatch):
    """A hand-edited settings.json with "hooks": "oops" or an event whose
    value isn't a list must not crash merge_hooks()."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    settings_json = tmp_path / ".claude" / "settings.json"
    settings_json.parent.mkdir(parents=True)
    settings_json.write_text(json.dumps({"hooks": {"Stop": "oops"}}))

    apply(
        "claude",
        _bundle(
            hooks=[
                DeclarativeHook(
                    event=HookEvent.STOP, command="witan session-checkpoint"
                )
            ]
        ),
    )

    settings = json.loads(settings_json.read_text())
    assert (
        settings["hooks"]["Stop"][0]["hooks"][0]["command"]
        == "witan session-checkpoint"
    )


def test_apply_claude_installs_skills(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    skill_md = tmp_path / "src" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("# skill")

    apply(
        "claude", _bundle(skills=[SkillSource(name="my-skill", skill_md_path=skill_md)])
    )

    dest = tmp_path / ".claude" / "skills" / "my-skill" / "SKILL.md"
    assert dest.read_text() == "# skill"


def test_apply_claude_ignores_plugin_hooks_meant_for_other_platforms(
    tmp_path, monkeypatch
):
    """Claude only merges DeclarativeHook entries into settings.json — a bundle
    that also carries a PluginRegistration entry (e.g. Pi's .ts extensions)
    must not have that file copied into settings.json as if it were a dir.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    plugin_src = tmp_path / "ext" / "witan.ts"
    plugin_src.parent.mkdir(parents=True)
    plugin_src.write_text("// stub")
    bundle = _bundle(
        hooks=[
            DeclarativeHook(event=HookEvent.STOP, command="witan session-checkpoint"),
            PluginRegistration(entry_path=plugin_src),
        ]
    )

    result = apply("claude", bundle)

    assert (tmp_path / ".claude" / "settings.json").is_file()
    assert not any("witan.ts" in str(p) for p in result.planned + result.written)


def test_apply_pi_registers_mcp_server_without_type_field(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    apply("pi", _bundle())

    entry = json.loads((tmp_path / ".pi" / "agent" / "mcp.json").read_text())[
        "mcpServers"
    ]["witan"]
    assert "type" not in entry
    assert entry["command"] == "uvx"


def test_apply_pi_copies_plugin_hooks_into_extensions_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    plugin_src = tmp_path / "ext" / "witan.ts"
    plugin_src.parent.mkdir(parents=True)
    plugin_src.write_text("// stub")

    apply("pi", _bundle(hooks=[PluginRegistration(entry_path=plugin_src)]))

    dest = tmp_path / ".pi" / "agent" / "extensions" / "witan.ts"
    assert dest.read_text() == "// stub"


def test_apply_pi_installs_skills_only_to_its_own_dir(tmp_path, monkeypatch):
    """Pi natively unions ~/.pi/agent/skills/ and ~/.agents/skills/ when
    discovering skills, so agent-config-kit must not also write into
    ~/.agents/skills/ — that would duplicate the skill and trigger Pi's own
    name-collision warning on every startup."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    skill_md = tmp_path / "src" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("# skill")

    apply("pi", _bundle(skills=[SkillSource(name="my-skill", skill_md_path=skill_md)]))

    assert (tmp_path / ".pi" / "agent" / "skills" / "my-skill" / "SKILL.md").exists()
    assert not (tmp_path / ".agents" / "skills" / "my-skill" / "SKILL.md").exists()


def test_apply_copilot_registers_mcp_server_under_servers_key_as_stdio(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    apply("copilot", _bundle())

    cfg = json.loads((vscode_user_dir() / "mcp.json").read_text())
    assert cfg["servers"]["witan"]["type"] == "stdio"


def test_apply_opencode_registers_mcp_server_folded_command_array(
    tmp_path, monkeypatch
):
    """Per OpenCode's real schema (McpLocalConfig): type="local", command+args
    folded into one array, and the env field is "environment" not "env"."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    apply("opencode", _bundle())

    entry = json.loads((tmp_path / ".config" / "opencode" / "config.json").read_text())[
        "mcp"
    ]["witan"]
    assert entry["type"] == "local"
    assert entry["command"] == ["uvx", "witan", "serve"]
    assert entry["environment"]["WITAN_AUTHOR"] == "tester"
    assert "env" not in entry
    assert "args" not in entry


def test_apply_project_scope_writes_relative_to_cwd_not_home(tmp_path, monkeypatch):
    """Project-scope targets are plain relative paths (registry.py) — they
    resolve against the process CWD, i.e. the repo root agent-kit is run from,
    never Path.home()."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "should-not-be-used")
    monkeypatch.chdir(tmp_path)

    apply("claude", _bundle(), scope=Scope.PROJECT)

    entry = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["witan"]
    assert entry["command"] == "uvx"
    assert not (tmp_path / "should-not-be-used").exists()


def test_apply_project_scope_installs_claude_skills_and_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.chdir(tmp_path)
    skill_md = tmp_path / "src" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("# skill")

    apply(
        "claude",
        _bundle(
            skills=[SkillSource(name="my-skill", skill_md_path=skill_md)],
            hooks=[DeclarativeHook(event=HookEvent.STOP, command="witan checkpoint")],
        ),
        scope=Scope.PROJECT,
    )

    assert (tmp_path / ".claude" / "skills" / "my-skill" / "SKILL.md").read_text() == (
        "# skill"
    )
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert settings["hooks"]["Stop"][0]["hooks"][0]["command"] == "witan checkpoint"


def test_apply_project_scope_pi_writes_dot_pi_not_dot_pi_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.chdir(tmp_path)

    apply("pi", _bundle(), scope=Scope.PROJECT)

    assert (tmp_path / ".pi" / "settings.json").is_file()
    assert not (tmp_path / ".pi" / "agent").exists()


def test_apply_project_scope_copilot_installs_skills_under_dot_github(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.chdir(tmp_path)
    skill_md = tmp_path / "src" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("# skill")

    apply(
        "copilot",
        _bundle(skills=[SkillSource(name="my-skill", skill_md_path=skill_md)]),
        scope=Scope.PROJECT,
    )

    assert (tmp_path / ".github" / "skills" / "my-skill" / "SKILL.md").read_text() == (
        "# skill"
    )


def test_apply_project_scope_opencode_writes_both_skill_dir_spellings(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.chdir(tmp_path)
    skill_md = tmp_path / "src" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("# skill")

    apply(
        "opencode",
        _bundle(skills=[SkillSource(name="my-skill", skill_md_path=skill_md)]),
        scope=Scope.PROJECT,
    )

    assert (tmp_path / ".opencode" / "skill" / "my-skill" / "SKILL.md").is_file()
    assert (tmp_path / ".opencode" / "skills" / "my-skill" / "SKILL.md").is_file()


def test_apply_all_only_touches_detected_platforms(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    results = apply_all(_bundle())

    assert set(results) == {"claude"}
    assert not (tmp_path / ".pi").exists()


def test_navigate_coerces_non_dict_intermediate_value_instead_of_raising():
    data = {"hooks": []}

    container = _navigate(data, ("hooks", "Stop"))

    assert container == {}
    assert data == {"hooks": {"Stop": {}}}


def test_navigate_leaves_existing_dict_values_untouched():
    data = {"mcpServers": {"other": {"command": "foo"}}}

    container = _navigate(data, ("mcpServers",))

    assert container is data["mcpServers"]
    assert container == {"other": {"command": "foo"}}


def test_merge_into_deep_merge_coerces_non_dict_existing_value():
    container = {"witan": "oops"}

    _merge_into(container, "witan", {"a": 1}, MergeStrategy.DEEP_MERGE)

    assert container["witan"] == {"a": 1}


def test_merge_into_deep_merge_merges_into_existing_dict():
    container = {"witan": {"a": 1}}

    _merge_into(container, "witan", {"b": 2}, MergeStrategy.DEEP_MERGE)

    assert container["witan"] == {"a": 1, "b": 2}
