"""Unit tests for ``witan setup`` per-agent installers.

These exercise config-file placement only and need no omnigraph binary.
"""

import json
from pathlib import Path

from witan import setup


def _run_install_claude(monkeypatch, home, *, author="tester", dry_run=False):
    monkeypatch.setattr(Path, "home", lambda: home)
    pkg_dir = home / "pkg"
    pkg_dir.mkdir()
    setup.install_claude(pkg_dir, author, dry_run=dry_run)


def test_install_claude_registers_mcp_server_in_claude_json(tmp_path, monkeypatch):
    """The MCP server goes in ~/.claude.json (where Claude Code reads it), as stdio."""
    _run_install_claude(monkeypatch, tmp_path)

    entry = json.loads((tmp_path / ".claude.json").read_text())["mcpServers"]["witan"]
    assert entry["type"] == "stdio"
    assert entry["command"] == "uvx"
    assert entry["env"]["WITAN_AUTHOR"] == "tester"


def test_install_claude_keeps_mcp_server_out_of_settings_json(tmp_path, monkeypatch):
    """settings.json receives hooks only — never the MCP server, which it would ignore."""
    _run_install_claude(monkeypatch, tmp_path)

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "mcpServers" not in settings
    hooks = settings["hooks"]
    assert any(
        h["command"] == "witan inject-context"
        for e in hooks["UserPromptSubmit"]
        for h in e["hooks"]
    )
    assert any(
        h["command"] == "witan session-checkpoint"
        for e in hooks["Stop"]
        for h in e["hooks"]
    )


def test_install_claude_preserves_existing_claude_json(tmp_path, monkeypatch):
    """Registration is additive: unrelated keys and other MCP servers survive."""
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        json.dumps({"numStartups": 7, "mcpServers": {"other": {"command": "foo"}}})
    )
    _run_install_claude(monkeypatch, tmp_path)

    cfg = json.loads(claude_json.read_text())
    assert cfg["numStartups"] == 7
    assert cfg["mcpServers"]["other"] == {"command": "foo"}
    assert "witan" in cfg["mcpServers"]


def test_install_claude_dry_run_writes_nothing(tmp_path, monkeypatch):
    """--dry-run reports intent without touching disk."""
    _run_install_claude(monkeypatch, tmp_path, dry_run=True)

    assert not (tmp_path / ".claude.json").exists()
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_load_json_object_rejects_non_object_json(tmp_path):
    """Valid-but-non-object JSON loads as None so callers skip it instead of crashing."""
    f = tmp_path / "config.json"
    f.write_text("[1, 2, 3]")
    assert setup._load_json_object(f) is None


def test_install_claude_leaves_non_object_claude_json_untouched(tmp_path, monkeypatch):
    """A ~/.claude.json holding non-object JSON is skipped, not clobbered or crashed on."""
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text("[1, 2, 3]")
    _run_install_claude(monkeypatch, tmp_path)

    assert json.loads(claude_json.read_text()) == [1, 2, 3]
