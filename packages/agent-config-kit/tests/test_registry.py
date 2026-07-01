from pathlib import Path

from agent_config_kit import registry


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
