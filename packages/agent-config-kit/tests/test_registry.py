from pathlib import Path

from agent_config_kit import registry
from agent_config_kit.models import ScopeTarget


def test_known_platforms_are_the_v1_populated_set():
    assert set(registry.known_platforms()) == {"claude", "pi", "copilot", "opencode"}


def test_claude_is_always_detected():
    assert "claude" in registry.detect_installed_platforms()


def test_detect_installed_platforms_skips_platforms_whose_marker_dir_is_absent(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert registry.detect_installed_platforms() == ["claude"]


def test_pi_registry_entry_notes_mcp_conditional_requirement():
    platform = registry.get_platform("pi")
    assert platform.mcp_conditional_on is not None


def test_claude_mcp_target_lives_under_claude_json(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    platform = registry.get_platform("claude")
    assert platform.mcp.global_.path == tmp_path / ".claude.json"
    assert platform.mcp.global_.key_path == ("mcpServers",)


def test_registry_is_rebuilt_fresh_so_home_directory_changes_are_reflected(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "first")
    first = registry.get_platform("claude").mcp.global_.path

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "second")
    second = registry.get_platform("claude").mcp.global_.path

    assert first != second


def test_claude_project_targets_are_relative_to_repo_root():
    platform = registry.get_platform("claude")

    assert platform.mcp.project == ScopeTarget(
        path=Path(".mcp.json"), key_path=("mcpServers",)
    )
    assert platform.hooks.project == ScopeTarget(
        path=Path(".claude") / "settings.json", key_path=("hooks",)
    )
    assert platform.skills.project == ScopeTarget(path=Path(".claude") / "skills")


def test_pi_project_targets_are_relative_to_repo_root():
    platform = registry.get_platform("pi")

    assert platform.mcp.project == ScopeTarget(
        path=Path(".pi") / "settings.json", key_path=("mcpServers",)
    )
    assert platform.hooks.project == ScopeTarget(path=Path(".pi") / "extensions")
    assert platform.skills.project == ScopeTarget(path=Path(".pi") / "skills")


def test_copilot_project_targets_are_relative_to_repo_root():
    platform = registry.get_platform("copilot")

    assert platform.mcp.project == ScopeTarget(
        path=Path(".vscode") / "mcp.json", key_path=("servers",)
    )
    assert platform.skills.project == ScopeTarget(path=Path(".github") / "skills")
    assert platform.skills.global_ is None


def test_opencode_project_targets_are_relative_to_repo_root():
    platform = registry.get_platform("opencode")

    assert platform.mcp.project == ScopeTarget(
        path=Path("opencode.json"), key_path=("mcp",)
    )
    assert platform.skills.project == ScopeTarget(path=Path(".opencode") / "skill")
    assert platform.skill_dest_dirs(platform.skills.project.path) == [
        Path(".opencode") / "skill",
        Path(".opencode") / "skills",
    ]


def test_every_platforms_global_targets_are_unaffected_by_project_additions(
    monkeypatch, tmp_path
):
    """Regression guard: adding project ScopeTargets must not disturb any
    platform's existing global target (the CLI/plan.py test suites rely on
    apply()'s scope=GLOBAL default resolving exactly as before)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert registry.get_platform("claude").mcp.global_.path == tmp_path / ".claude.json"
    assert (
        registry.get_platform("pi").mcp.global_.path
        == tmp_path / ".pi" / "agent" / "mcp.json"
    )
    assert (
        registry.get_platform("opencode").mcp.global_.path
        == tmp_path / ".config" / "opencode" / "config.json"
    )
