import json
from pathlib import Path

from agent_config_kit.diff import diff
from agent_config_kit.models import (
    DeclarativeHook,
    HookEvent,
    PluginRegistration,
    SkillSource,
    StdioServer,
)
from agent_config_kit.plan import RegistrationBundle, apply


def _bundle(**overrides) -> RegistrationBundle:
    defaults = dict(
        mcp_servers={"witan": StdioServer(command="uvx", args=["witan", "serve"])},
    )
    defaults.update(overrides)
    return RegistrationBundle(**defaults)


def test_diff_reports_no_drift_when_already_applied(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    bundle = _bundle()
    apply("claude", bundle)

    result = diff("claude", bundle)

    assert not result.has_drift
    assert result.missing_keys == []
    assert result.mismatched_keys == []
    assert result.unreadable_paths == []


def test_diff_resolves_per_platform_mcp_overrides(tmp_path, monkeypatch):
    """diff() has to apply the same override resolution apply() does, or an
    overridden platform reads as permanent drift right after being applied."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    bundle = _bundle(
        mcp_servers_by_platform={
            "claude": {"witan": StdioServer(command="witan", args=["serve"])}
        }
    )
    apply("claude", bundle)

    assert not diff("claude", bundle).has_drift


def test_diff_reports_missing_mcp_server(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    result = diff("claude", _bundle())

    assert result.has_drift
    assert result.missing_keys == ["mcp_servers:witan"]


def test_diff_reports_mismatched_mcp_server(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        json.dumps(
            {"mcpServers": {"witan": {"type": "stdio", "command": "old-command"}}}
        )
    )

    result = diff("claude", _bundle())

    assert result.has_drift
    assert result.mismatched_keys == ["mcp_servers:witan"]
    assert result.missing_keys == []


def test_diff_reports_unreadable_json_distinctly_not_as_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude.json").write_text("[1, 2, 3]")

    result = diff("claude", _bundle())

    assert not result.has_drift
    assert result.unreadable_paths == [tmp_path / ".claude.json"]


def test_diff_never_writes_to_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    diff("claude", _bundle())

    assert not (tmp_path / ".claude.json").exists()


def test_diff_reports_missing_declarative_hook(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    bundle = _bundle(
        mcp_servers={},
        hooks=[
            DeclarativeHook(event=HookEvent.STOP, command="witan session-checkpoint")
        ],
    )

    result = diff("claude", bundle)

    assert result.missing_keys == ["hooks:stop:witan session-checkpoint"]


def test_diff_reports_no_drift_for_already_applied_hook(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    bundle = _bundle(
        hooks=[
            DeclarativeHook(event=HookEvent.STOP, command="witan session-checkpoint")
        ]
    )
    apply("claude", bundle)

    result = diff("claude", bundle)

    assert not result.has_drift


def test_diff_reports_missing_plugin_hook_file(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    plugin_src = tmp_path / "ext" / "witan.ts"
    plugin_src.parent.mkdir(parents=True)
    plugin_src.write_text("// stub")

    result = diff("pi", _bundle(hooks=[PluginRegistration(entry_path=plugin_src)]))

    assert result.missing_paths == [
        tmp_path / ".pi" / "agent" / "extensions" / "witan.ts"
    ]


def test_diff_reports_missing_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    skill_md = tmp_path / "src" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("# skill")

    result = diff(
        "claude", _bundle(skills=[SkillSource(name="my-skill", skill_md_path=skill_md)])
    )

    assert result.missing_paths == [
        tmp_path / ".claude" / "skills" / "my-skill" / "SKILL.md"
    ]


def test_diff_reports_no_drift_for_already_installed_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    skill_md = tmp_path / "src" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("# skill")
    bundle = _bundle(skills=[SkillSource(name="my-skill", skill_md_path=skill_md)])
    apply("claude", bundle)

    result = diff("claude", bundle)

    assert not result.has_drift


def test_diff_reports_missing_supporting_file_as_drift(tmp_path, monkeypatch):
    """A skill installed before it gained a scripts/ file (or one that was
    only partially copied) must show up as drift on the missing file, not
    just as "installed" because SKILL.md itself is present."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "SKILL.md").write_text("# skill")
    skill = SkillSource(name="my-skill", skill_md_path=src_dir / "SKILL.md")
    bundle = _bundle(skills=[skill])
    apply("claude", bundle)
    # Simulate the skill gaining a script after the last apply.
    (src_dir / "scripts").mkdir()
    (src_dir / "scripts" / "run.sh").write_text("#!/bin/sh\necho hi\n")

    result = diff("claude", bundle)

    assert result.has_drift
    assert result.missing_paths == [
        tmp_path / ".claude" / "skills" / "my-skill" / "scripts" / "run.sh"
    ]
