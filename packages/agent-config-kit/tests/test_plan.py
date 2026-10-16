import json
import tomllib
from pathlib import Path

import pytest

from agent_config_kit.manifest import load_manifest
from agent_config_kit.models import (
    DeclarativeHook,
    HookEvent,
    MergeStrategy,
    PluginRegistration,
    RemoteServer,
    Scope,
    SkillSource,
    StdioServer,
)
from agent_config_kit.paths import vscode_user_dir
from agent_config_kit.plan import (
    HookSkillError,
    RegistrationBundle,
    _merge_into,
    _navigate,
    apply,
    apply_all,
    platform_hooks,
    skills_shadowed_by,
)
from agent_config_kit.prune import PlatformState, apply_with_prune


def _bundle(**overrides) -> RegistrationBundle:
    defaults = {
        "mcp_servers": {
            "witan": StdioServer(
                command="uvx", args=["witan", "serve"], env={"WITAN_AUTHOR": "tester"}
            )
        },
    }
    defaults.update(overrides)
    return RegistrationBundle(**defaults)


def test_apply_claude_registers_mcp_server_as_stdio(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    apply("claude", _bundle())

    entry = json.loads((tmp_path / ".claude.json").read_text())["mcpServers"]["witan"]
    assert entry["type"] == "stdio"
    assert entry["command"] == "uvx"
    assert entry["env"]["WITAN_AUTHOR"] == "tester"


def test_apply_records_diff_for_a_newly_added_mcp_server(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    result = apply("claude", _bundle())

    [(path, diff)] = result.diffs
    assert path == tmp_path / ".claude.json"
    assert diff.startswith("--- before")
    assert '+    "witan"' in diff


def test_apply_dry_run_still_records_the_diff(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    result = apply("claude", _bundle(), dry_run=True)

    assert not (tmp_path / ".claude.json").exists()
    assert len(result.diffs) == 1


def test_apply_records_no_diff_when_reapplying_unchanged_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    bundle = _bundle()
    apply("claude", bundle)

    result = apply("claude", bundle)

    assert result.diffs == []


def test_apply_records_diff_for_a_changed_mcp_server_value(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    apply("claude", _bundle())

    result = apply(
        "claude",
        _bundle(
            mcp_servers={
                "witan": StdioServer(
                    command="uvx", args=["witan", "serve", "--verbose"]
                )
            }
        ),
    )

    [(_, diff)] = result.diffs
    assert '-    "args"' not in diff  # unified diff shows changed lines, not headers
    assert "--verbose" in diff


def test_mcp_servers_by_platform_overrides_only_the_named_platform(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".pi").mkdir()
    bundle = _bundle(
        mcp_servers_by_platform={
            "claude": {"witan": StdioServer(command="witan", args=["serve"])}
        }
    )

    apply("claude", bundle)
    apply("pi", bundle)

    claude = json.loads((tmp_path / ".claude.json").read_text())["mcpServers"]["witan"]
    assert (claude["command"], claude["args"]) == ("witan", ["serve"])
    pi = json.loads((tmp_path / ".pi" / "agent" / "mcp.json").read_text())
    assert pi["mcpServers"]["witan"]["command"] == "uvx"


def test_mcp_servers_by_platform_merges_by_key_leaving_siblings_alone(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    bundle = _bundle(
        mcp_servers={
            "witan": StdioServer(command="uvx", args=["witan", "serve"]),
            "other": StdioServer(command="uvx", args=["other", "serve"]),
        },
        mcp_servers_by_platform={
            "claude": {"witan": StdioServer(command="witan", args=["serve"])}
        },
    )

    apply("claude", bundle)

    servers = json.loads((tmp_path / ".claude.json").read_text())["mcpServers"]
    assert servers["witan"]["command"] == "witan"
    assert servers["other"]["command"] == "uvx"


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


def test_apply_records_diff_for_a_newly_merged_declarative_hook(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    bundle = _bundle(
        mcp_servers={},
        hooks=[
            DeclarativeHook(
                event=HookEvent.USER_PROMPT_SUBMIT, command="witan inject-context"
            )
        ],
    )

    result = apply("claude", bundle)
    rerun = apply("claude", bundle)  # re-applying must not duplicate entries

    [(path, diff)] = result.diffs
    assert path == tmp_path / ".claude" / "settings.json"
    assert "witan inject-context" in diff
    assert rerun.diffs == []


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

    assert (tmp_path / ".pi" / "mcp.json").is_file()
    assert not (tmp_path / ".pi" / "agent").exists()


def test_apply_project_scope_pi_writes_mcp_to_dot_pi_mcp_json(tmp_path, monkeypatch):
    """Regression: Pi (and pi-mcp-adapter) read project MCP servers from
    ``.pi/mcp.json``. ``.pi/settings.json`` is Pi core's own settings file,
    which holds no MCP servers — a project-scoped apply must not create it."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.chdir(tmp_path)

    result = apply("pi", _bundle(), scope=Scope.PROJECT)

    mcp_json = Path(".pi") / "mcp.json"
    assert result.written == [mcp_json]
    assert json.loads((tmp_path / mcp_json).read_text()) == {
        "mcpServers": {
            "witan": {
                "command": "uvx",
                "args": ["witan", "serve"],
                "env": {"WITAN_AUTHOR": "tester"},
            }
        }
    }
    assert not (tmp_path / ".pi" / "settings.json").exists()
    assert not (tmp_path / "home" / ".pi" / "agent" / "mcp.json").exists()


def test_apply_project_scope_pi_preserves_existing_pi_mcp_json_entries(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".pi").mkdir()
    (tmp_path / ".pi" / "mcp.json").write_text(
        json.dumps(
            {"settings": {"toolPrefix": "short"}, "mcpServers": {"x": {"url": "u"}}}
        )
    )

    apply("pi", _bundle(), scope=Scope.PROJECT)

    cfg = json.loads((tmp_path / ".pi" / "mcp.json").read_text())
    assert cfg["settings"] == {"toolPrefix": "short"}
    assert set(cfg["mcpServers"]) == {"x", "witan"}


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


_CWD_HEADERS_MANIFEST = """
[mcp_servers.local]
kind = "stdio"
command = "uv"
args = ["run", "server"]
cwd = "/srv/local-server"

[mcp_servers.hosted]
kind = "remote"
url = "https://example.com/mcp"
headers = { Authorization = "Bearer super-secret-token" }
"""


def test_manifest_server_cwd_and_headers_project_to_both_claude_and_pi(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    manifest = tmp_path / "agent-config.toml"
    manifest.write_text(_CWD_HEADERS_MANIFEST)
    bundle = load_manifest(manifest).bundle

    apply("claude", bundle)
    apply("pi", bundle)

    claude = json.loads((tmp_path / ".claude.json").read_text())["mcpServers"]
    pi = json.loads((tmp_path / ".pi" / "agent" / "mcp.json").read_text())["mcpServers"]
    assert claude["local"]["cwd"] == pi["local"]["cwd"] == "/srv/local-server"
    assert (
        claude["hosted"]["headers"]
        == pi["hosted"]["headers"]
        == {"Authorization": "Bearer super-secret-token"}
    )
    assert pi == {
        "local": {
            "command": "uv",
            "args": ["run", "server"],
            "cwd": "/srv/local-server",
        },
        "hosted": {
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer super-secret-token"},
        },
    }


def test_apply_pi_diff_redacts_header_values(tmp_path, monkeypatch):
    """Pi-specific redaction regression: headers only reach a Pi diff now that
    the Pi adapter emits them, so the diff printed by ``apply --diff`` must
    still blank their values (dry-run and real apply alike)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    manifest = tmp_path / "agent-config.toml"
    manifest.write_text(_CWD_HEADERS_MANIFEST)
    bundle = load_manifest(manifest).bundle

    for dry_run in (True, False):
        [(path, diff)] = apply("pi", bundle, dry_run=dry_run).diffs
        assert path == tmp_path / ".pi" / "agent" / "mcp.json"
        assert '"headers"' in diff
        assert '"Authorization": "<redacted>"' in diff
        assert "super-secret-token" not in diff
        assert "/srv/local-server" in diff  # cwd is not a secret


def test_apply_codex_registers_mcp_server_in_config_toml(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    apply("codex", _bundle())

    cfg = tomllib.loads((tmp_path / ".codex" / "config.toml").read_text())
    assert cfg["mcp_servers"]["witan"] == {
        "command": "uvx",
        "args": ["witan", "serve"],
        "env": {"WITAN_AUTHOR": "tester"},
    }


def test_apply_codex_preserves_hand_edits_in_config_toml(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    existing = (
        "# my settings\n"
        'model = "gpt-5"\n'
        "\n"
        "[mcp_servers.other]\n"
        'command = "foo"  # keep me\n'
        "enabled = true\n"
    )
    config.write_text(existing)

    result = apply("codex", _bundle())

    text = config.read_text()
    assert text.startswith(existing)
    assert tomllib.loads(text)["mcp_servers"]["witan"]["command"] == "uvx"
    assert result.diffs  # tomlkit values (e.g. its Bool) must diff cleanly


def test_apply_codex_skips_unparseable_config_toml(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text("not = [valid toml")

    result = apply("codex", _bundle())

    assert result.skipped == [(config, "could not parse as a config object")]
    assert config.read_text() == "not = [valid toml"


def test_apply_codex_remote_server_maps_headers_and_oauth(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    bundle = RegistrationBundle(
        mcp_servers={
            "tsw": RemoteServer(
                url="https://example.test/mcp",
                headers={"X-Team": "ol"},
                oauth={"clientId": "toolhive-swe-cli", "callbackPort": 8080},
            )
        }
    )

    apply("codex", bundle)

    cfg = tomllib.loads((tmp_path / ".codex" / "config.toml").read_text())
    assert cfg["mcp_servers"]["tsw"] == {
        "url": "https://example.test/mcp",
        "http_headers": {"X-Team": "ol"},
        "oauth": {
            "client_id": "toolhive-swe-cli",
            "callback_port": 8080,
            "callback_url": "http://localhost:8080/callback",
        },
    }


def test_apply_codex_keeps_an_explicit_oauth_callback_url(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    bundle = RegistrationBundle(
        mcp_servers={
            "tsw": RemoteServer(
                url="https://example.test/mcp",
                oauth={
                    "clientId": "c",
                    "callbackPort": 8080,
                    "callbackUrl": "http://127.0.0.1:8080/cb",
                },
            )
        }
    )

    apply("codex", bundle)

    cfg = tomllib.loads((tmp_path / ".codex" / "config.toml").read_text())
    assert cfg["mcp_servers"]["tsw"]["oauth"]["callback_url"] == (
        "http://127.0.0.1:8080/cb"
    )


def test_apply_codex_installs_skills_under_dot_agents(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    skill_md = tmp_path / "src" / "my-skill" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("---\nname: my-skill\n---\n")

    apply(
        "codex",
        _bundle(skills=[SkillSource(name="my-skill", skill_md_path=skill_md)]),
    )

    assert (tmp_path / ".agents" / "skills" / "my-skill" / "SKILL.md").is_file()


def test_apply_project_scope_codex_writes_dot_codex_config_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    apply("codex", _bundle(), scope=Scope.PROJECT)

    cfg = tomllib.loads((tmp_path / ".codex" / "config.toml").read_text())
    assert "witan" in cfg["mcp_servers"]


def test_apply_codex_diff_redacts_header_values(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    bundle = RegistrationBundle(
        mcp_servers={
            "tsw": RemoteServer(
                url="https://example.test/mcp",
                headers={"Authorization": "Bearer SECRET123"},
            )
        }
    )

    result = apply("codex", bundle, dry_run=True)

    assert result.diffs
    assert all("SECRET123" not in diff for _, diff in result.diffs)


def test_apply_codex_keeps_top_level_keys_after_dotted_mcp_servers(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('mcp_servers.mine.command = "x"\nmodel = "o3"\n')

    apply("codex", _bundle())

    cfg = tomllib.loads(config.read_text())
    assert cfg["model"] == "o3"
    assert cfg["mcp_servers"]["mine"] == {"command": "x"}
    assert cfg["mcp_servers"]["witan"]["command"] == "uvx"


def test_apply_all_skips_pi_skills_when_codex_is_detected(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".pi").mkdir()
    (tmp_path / ".codex").mkdir()
    skill_md = tmp_path / "src" / "my-skill" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("---\nname: my-skill\n---\n")

    apply_all(_bundle(skills=[SkillSource(name="my-skill", skill_md_path=skill_md)]))

    assert (tmp_path / ".agents" / "skills" / "my-skill" / "SKILL.md").is_file()
    assert not (tmp_path / ".pi" / "agent" / "skills" / "my-skill").exists()


def test_skills_shadowed_by_applies_at_project_scope_too():
    assert skills_shadowed_by("pi", ["pi", "codex"], Scope.PROJECT) == "codex"
    assert skills_shadowed_by("pi", ["pi", "claude"], Scope.PROJECT) is None
    assert skills_shadowed_by("codex", ["pi", "codex"], Scope.GLOBAL) is None


def test_apply_codex_tolerates_nan_in_config_toml(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text("temp = nan\n")

    apply("codex", _bundle())

    assert "witan" in tomllib.loads(config.read_text())["mcp_servers"]


def _skill(tmp_path: Path, name: str = "preflight") -> SkillSource:
    skill_md = tmp_path / "src" / name / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text(f"---\nname: {name}\n---\n")
    return SkillSource(name=name, skill_md_path=skill_md)


def _skill_hook() -> DeclarativeHook:
    return DeclarativeHook(
        event=HookEvent.SESSION_START,
        command="python3 {skill_dir}/scripts/check.py",
        skill="preflight",
    )


def _codex_hooks(tmp_path: Path) -> dict:
    return json.loads((tmp_path / ".codex" / "hooks.json").read_text())["hooks"]


def test_apply_codex_writes_declarative_hooks_to_hooks_json(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    bundle = _bundle(
        mcp_servers={},
        hooks=[
            DeclarativeHook(
                event=HookEvent.SESSION_START,
                command="witan inject-context",
                timeout_seconds=7.5,
            )
        ],
    )

    result = apply("codex", bundle)

    assert _codex_hooks(tmp_path) == {
        "SessionStart": [
            {
                "matcher": "",
                # Codex reads timeout as whole seconds and rejects 7.5.
                "hooks": [
                    {"type": "command", "command": "witan inject-context", "timeout": 8}
                ],
            }
        ]
    }
    assert result.written == [tmp_path / ".codex" / "hooks.json"]
    assert not (tmp_path / ".codex" / "config.toml").exists()


def test_apply_codex_merges_with_users_own_hooks_and_dedups(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    hooks_json = tmp_path / ".codex" / "hooks.json"
    hooks_json.parent.mkdir()
    users_own = {
        "matcher": "startup",
        "hooks": [{"type": "command", "command": "mine"}],
    }
    hooks_json.write_text(
        json.dumps(
            {"description": "hand-written", "hooks": {"SessionStart": [users_own]}}
        )
    )
    bundle = _bundle(
        mcp_servers={},
        hooks=[DeclarativeHook(event=HookEvent.SESSION_START, command="witan ctx")],
    )

    apply("codex", bundle)
    rerun = apply("codex", bundle)

    data = json.loads(hooks_json.read_text())
    assert data["description"] == "hand-written"
    session_start = data["hooks"]["SessionStart"]
    assert session_start[0] == users_own
    assert [h["command"] for e in session_start for h in e["hooks"]] == [
        "mine",
        "witan ctx",
    ]
    assert rerun.diffs == []


def test_apply_codex_reports_one_approval_notice_per_new_or_changed_hook(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    hook = DeclarativeHook(event=HookEvent.SESSION_START, command="witan ctx")
    other = DeclarativeHook(event=HookEvent.STOP, command="witan checkpoint")
    bundle = _bundle(mcp_servers={}, hooks=[hook, other])

    dry = apply("codex", bundle, dry_run=True)
    first = apply("codex", bundle)
    rerun = apply("codex", bundle)
    retimed = apply(
        "codex",
        _bundle(
            mcp_servers={},
            hooks=[hook, other.model_copy(update={"timeout_seconds": 30})],
        ),
    )

    assert len(dry.notices) == 2
    assert "would be written" in dry.notices[0]
    assert len(first.notices) == 2
    assert "'witan ctx' was written to" in first.notices[0]
    assert "/hooks" in first.notices[0]
    assert rerun.notices == []
    [notice] = retimed.notices
    assert "witan checkpoint" in notice


def test_apply_claude_reports_no_approval_notices(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    hook = DeclarativeHook(event=HookEvent.SESSION_START, command="witan ctx")

    assert apply("claude", _bundle(mcp_servers={}, hooks=[hook])).notices == []


def test_apply_codex_never_writes_hook_trust_state(tmp_path, monkeypatch):
    """Codex runs a user hook only once its hash matches a trusted_hash the
    user approved; writing one ourselves would skip that approval."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    bundle = _bundle(
        skills=[_skill(tmp_path)],
        hooks=[
            DeclarativeHook(event=HookEvent.SESSION_START, command="witan ctx"),
            _skill_hook(),
        ],
    )

    apply("codex", bundle)

    codex_files = [p for p in (tmp_path / ".codex").rglob("*") if p.is_file()]
    assert {p.name for p in codex_files} == {"config.toml", "hooks.json"}
    assert not any("trusted_hash" in p.read_text() for p in codex_files)
    config = tomllib.loads((tmp_path / ".codex" / "config.toml").read_text())
    assert set(config) == {"mcp_servers"}
    hooks_file = json.loads((tmp_path / ".codex" / "hooks.json").read_text())
    assert set(hooks_file) == {"hooks"}


def test_apply_codex_has_no_project_scope_hooks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bundle = _bundle(
        mcp_servers={},
        hooks=[DeclarativeHook(event=HookEvent.SESSION_START, command="witan ctx")],
    )

    result = apply("codex", bundle, scope=Scope.PROJECT)

    assert result.planned == []
    assert not (tmp_path / ".codex").exists()


def test_apply_substitutes_skill_dir_per_platform(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    bundle = _bundle(mcp_servers={}, skills=[_skill(tmp_path)], hooks=[_skill_hook()])

    apply("claude", bundle)
    apply("codex", bundle)

    claude = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    [claude_entry] = claude["hooks"]["SessionStart"]
    assert claude_entry["hooks"][0]["command"] == (
        f"python3 {tmp_path}/.claude/skills/preflight/scripts/check.py"
    )
    [codex_entry] = _codex_hooks(tmp_path)["SessionStart"]
    assert codex_entry["hooks"][0]["command"] == (
        f"python3 {tmp_path}/.agents/skills/preflight/scripts/check.py"
    )
    assert (tmp_path / ".agents" / "skills" / "preflight" / "SKILL.md").is_file()


def test_apply_substitutes_skill_dir_at_project_scope(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bundle = _bundle(mcp_servers={}, skills=[_skill(tmp_path)], hooks=[_skill_hook()])

    apply("claude", bundle, scope=Scope.PROJECT)

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    [entry] = settings["hooks"]["SessionStart"]
    assert entry["hooks"][0]["command"] == (
        "python3 .claude/skills/preflight/scripts/check.py"
    )


def test_apply_errors_when_a_hooks_skill_is_not_installed_this_run(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    bundle = _bundle(mcp_servers={}, hooks=[_skill_hook()])

    with pytest.raises(HookSkillError, match="'preflight'"):
        apply("codex", bundle)
    assert not (tmp_path / ".codex").exists()


def test_apply_raises_for_a_missing_skill_before_writing_mcp_servers(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    bundle = _bundle(hooks=[_skill_hook()])

    with pytest.raises(HookSkillError):
        apply("codex", bundle)
    with pytest.raises(HookSkillError):
        apply_with_prune("claude", bundle, PlatformState())

    assert not (tmp_path / ".codex" / "config.toml").exists()
    assert not (tmp_path / ".claude.json").exists()


def test_skill_hooks_are_not_resolved_where_the_scope_has_no_hooks_target(
    tmp_path, monkeypatch
):
    """Codex has no project-scope hooks file, so a skill hook that would
    never be written there must not fail an otherwise valid run."""
    monkeypatch.chdir(tmp_path)
    bundle = _bundle(hooks=[_skill_hook()])

    assert platform_hooks("codex", bundle, Scope.PROJECT) == []
    apply("codex", bundle, scope=Scope.PROJECT)

    assert (
        "witan"
        in tomllib.loads((tmp_path / ".codex" / "config.toml").read_text())[
            "mcp_servers"
        ]
    )


def test_apply_all_checks_skill_hooks_before_writing_any_platform(
    tmp_path, monkeypatch
):
    """apply() writes MCP servers before it reaches hooks, so without the
    up-front check Claude's .claude.json would be written before the
    missing skill raises."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".codex").mkdir()
    bundle = _bundle(hooks=[_skill_hook()])

    with pytest.raises(HookSkillError):
        apply_all(bundle)

    assert not (tmp_path / ".claude.json").exists()
    assert not (tmp_path / ".codex" / "config.toml").exists()


def test_platform_hooks_skips_skill_hooks_where_declarative_hooks_never_go(
    tmp_path, monkeypatch
):
    """Pi's hooks are plugin files: a {skill_dir} hook never reaches it, so a
    skill Pi skips (because Codex installs it) can't fail Pi's run."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    plugin = PluginRegistration(entry_path=tmp_path / "witan.ts")
    bundle = _bundle(mcp_servers={}, hooks=[_skill_hook(), plugin])

    assert platform_hooks("pi", bundle, Scope.GLOBAL) == [plugin]
    assert platform_hooks("copilot", bundle, Scope.GLOBAL) == []
