"""Integration tests for witan's own registration bundle + starter config.

Generic install-mechanics behavior (dry-run no-op, additive merge, JSON
skip-not-crash, hook dedup, ...) is covered by agent-config-kit's own test
suite (``packages/agent-config-kit/tests/``); the shared omnigraph installer is
covered by ``packages/witan-core/tests/test_omnigraph_install.py``. This only
asserts that ``witan_bundle()`` + ``apply("claude", ...)`` produces *witan's*
MCP entry and hook commands, and that the starter-config writer behaves.
"""

import json
from pathlib import Path

from agent_config_kit import apply

from witan import setup


def test_witan_bundle_registers_witan_mcp_server_and_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()

    bundle = setup.witan_bundle(pkg_dir, "tester")
    apply("claude", bundle)

    claude_json = json.loads((tmp_path / ".claude.json").read_text())
    entry = claude_json["mcpServers"]["witan"]
    assert entry["type"] == "stdio"
    # Claude Code's hooks already require `witan` on PATH, so the MCP entry runs
    # that same install rather than a second one resolved from git `main`.
    assert entry["command"] == "witan"
    assert entry["args"] == ["serve"]
    assert entry["env"]["WITAN_AUTHOR"] == "tester"

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    inject = [
        h
        for e in settings["hooks"]["UserPromptSubmit"]
        for h in e["hooks"]
        if h["command"] == "witan inject-context"
    ]
    checkpoint = [
        h
        for e in settings["hooks"]["Stop"]
        for h in e["hooks"]
        if h["command"] == "witan session-checkpoint"
    ]
    assert inject and checkpoint
    # Both prompt-path hooks carry a timeout so a hung git/graph can't stall.
    assert inject[0]["timeout"] == 15
    assert checkpoint[0]["timeout"] == 15


def test_mcp_only_platforms_keep_the_self_contained_uvx_entry(tmp_path, monkeypatch):
    """Copilot has no witan hooks and so no CLI to point at — it still needs the
    uvx form, which is why the CLI entry is a per-platform override rather than
    a wholesale replacement."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    vscode_dir = tmp_path / ".config" / "Code" / "User"
    vscode_dir.mkdir(parents=True)
    monkeypatch.setattr("agent_config_kit.registry.vscode_user_dir", lambda: vscode_dir)
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()

    apply("copilot", setup.witan_bundle(pkg_dir, "tester"))

    entry = json.loads((vscode_dir / "mcp.json").read_text())["servers"]["witan"]
    assert entry["command"] == "uvx"
    assert "--from" in entry["args"]


def test_witan_bundle_includes_pi_extensions_as_plugin_hooks(tmp_path):
    pkg_dir = tmp_path / "pkg"
    ext_dir = pkg_dir / "extensions" / "pi"
    ext_dir.mkdir(parents=True)
    (ext_dir / "witan.ts").write_text("// stub")

    bundle = setup.witan_bundle(pkg_dir, "tester")

    plugin_hooks = [h for h in bundle.hooks if hasattr(h, "entry_path")]
    assert any(h.entry_path.name == "witan.ts" for h in plugin_hooks)


def test_witan_bundle_includes_bundled_skills(tmp_path):
    pkg_dir = tmp_path / "pkg"
    skill_dir = pkg_dir / "skills" / "witan-task"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# witan-task")

    bundle = setup.witan_bundle(pkg_dir, "tester")

    assert any(s.name == "witan-task" for s in bundle.skills)


def test_install_default_config_writes_starter_file(tmp_path, monkeypatch):
    import tomllib

    from witan import config as cfg_module

    dest = tmp_path / "config.toml"
    monkeypatch.setattr(cfg_module, "DEFAULT_CONFIG_PATH", dest)

    setup.install_default_config(dry_run=False)

    assert dest.exists()
    parsed = tomllib.loads(dest.read_text())
    assert parsed == {"rank": {}, "scan": {}}  # everything ships commented out


def test_install_default_config_skips_existing_file(tmp_path, monkeypatch):
    from witan import config as cfg_module

    dest = tmp_path / "config.toml"
    dest.write_text("author = 'do not touch'\n")
    monkeypatch.setattr(cfg_module, "DEFAULT_CONFIG_PATH", dest)

    setup.install_default_config(dry_run=False)

    assert dest.read_text() == "author = 'do not touch'\n"


def test_install_default_config_dry_run_writes_nothing(tmp_path, monkeypatch):
    from witan import config as cfg_module

    dest = tmp_path / "config.toml"
    monkeypatch.setattr(cfg_module, "DEFAULT_CONFIG_PATH", dest)

    setup.install_default_config(dry_run=True)

    assert not dest.exists()


# ── Legacy workflow-hook prune (A1 double-emission fix) ──────────────────────


def _settings_with(commands_by_event):
    return {
        "hooks": {
            event: [
                {"matcher": "", "hooks": [{"type": "command", "command": c}]}
                for c in commands
            ]
            for event, commands in commands_by_event.items()
        }
    }


def test_prune_removes_wrapper_keeps_bare():
    settings = _settings_with(
        {
            "UserPromptSubmit": [
                "witan inject-context",
                "bash ~/.claude/hooks/workflow-context-inject.sh",
            ],
            "Stop": ["bash ~/.claude/hooks/workflow-session-checkpoint.sh"],
        }
    )
    changed = setup.prune_legacy_hook_entries(settings)
    assert changed is True
    ups = settings["hooks"]["UserPromptSubmit"]
    assert [e["hooks"][0]["command"] for e in ups] == ["witan inject-context"]
    # Stop had only the wrapper → matcher entry dropped entirely, no empty hooks list.
    assert settings["hooks"]["Stop"] == []


def test_prune_matches_configs_hooks_form():
    settings = _settings_with(
        {
            "UserPromptSubmit": ["bash $REPO/configs/hooks/workflow-context-inject.sh"],
        }
    )
    assert setup.prune_legacy_hook_entries(settings) is True
    assert settings["hooks"]["UserPromptSubmit"] == []


def test_prune_idempotent_and_leaves_others():
    settings = _settings_with(
        {
            "UserPromptSubmit": ["witan inject-context"],
            "PostToolUse": ["witan-code reindex-hook"],
        }
    )
    assert setup.prune_legacy_hook_entries(settings) is False
    assert setup.prune_legacy_hook_entries(settings) is False
    assert len(settings["hooks"]["PostToolUse"]) == 1


def test_prune_no_hooks_section_is_noop():
    assert setup.prune_legacy_hook_entries({}) is False
