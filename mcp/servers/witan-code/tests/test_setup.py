"""Tests for witan-code's registration bundle.

Generic install-mechanics behavior (dry-run no-op, additive merge, JSON
skip-not-crash, hook dedup, ...) is covered by agent-config-kit's own test
suite (``packages/agent-config-kit/tests/``); the shared omnigraph installer is
covered by ``packages/witan-core/tests/test_omnigraph_install.py``. The bundle
tests here only assert that ``witan_code_bundle()`` + ``apply("claude", ...)``
produces witan-code's own MCP entry and hook commands, i.e. that the wiring is
correct.
"""

import json
from pathlib import Path

from agent_config_kit import apply

from witan_code import setup


def test_witan_code_bundle_registers_mcp_server_and_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()

    bundle = setup.witan_code_bundle(pkg_dir, "tester")
    apply("claude", bundle)

    claude_json = json.loads((tmp_path / ".claude.json").read_text())
    entry = claude_json["mcpServers"]["witan-code"]
    assert entry["type"] == "stdio"
    assert entry["command"] == "uvx"
    assert entry["env"]["WITAN_AUTHOR"] == "tester"

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    session_init = [
        h
        for e in settings["hooks"]["SessionStart"]
        for h in e["hooks"]
        if h["command"] == "witan-code session-init"
    ]
    reindex = [
        h
        for e in settings["hooks"]["PostToolUse"]
        for h in e["hooks"]
        if h["command"] == "witan-code reindex-hook"
    ]
    context = [
        h
        for e in settings["hooks"]["UserPromptSubmit"]
        for h in e["hooks"]
        if h["command"] == "witan-code inject-context"
    ]
    checkpoint = [
        h
        for e in settings["hooks"]["Stop"]
        for h in e["hooks"]
        if h["command"] == "witan-code checkpoint"
    ]
    assert session_init and reindex and context and checkpoint
    # Both prompt-path hooks carry a timeout so a hung git/store can't stall.
    assert context[0]["timeout"] == 15
    assert checkpoint[0]["timeout"] == 15


def test_witan_code_bundle_honors_binary_override(tmp_path):
    """witan.cli.setup_cmd passes binary="witan code" when folding this
    bundle into witan's own, so hooks only need `witan` on PATH — not a
    separately installed `witan-code` binary."""
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()

    bundle = setup.witan_code_bundle(pkg_dir, "tester", binary="witan code")

    commands = {h.command for h in bundle.hooks if hasattr(h, "command")}
    assert commands == {
        "witan code session-init",
        "witan code reindex-hook",
        "witan code inject-context",
        "witan code checkpoint",
    }


def test_witan_code_bundle_includes_pi_extensions_as_plugin_hooks(tmp_path):
    pkg_dir = tmp_path / "pkg"
    ext_dir = pkg_dir / "extensions" / "pi"
    ext_dir.mkdir(parents=True)
    (ext_dir / "codegraph.ts").write_text("// stub")

    bundle = setup.witan_code_bundle(pkg_dir, "tester")

    plugin_hooks = [h for h in bundle.hooks if hasattr(h, "entry_path")]
    assert any(h.entry_path.name == "codegraph.ts" for h in plugin_hooks)


def test_witan_code_bundle_includes_bundled_skills(tmp_path):
    pkg_dir = tmp_path / "pkg"
    skill_dir = pkg_dir / "skills" / "witan-code"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# witan-code")

    bundle = setup.witan_code_bundle(pkg_dir, "tester")

    assert any(s.name == "witan-code" for s in bundle.skills)


def test_setup_does_not_abort_on_a_refused_omnigraph_binary(tmp_path, monkeypatch):
    """★ Same contract as `witan setup`, and worth asserting separately.

    The installer raises by default as of witan-core 0.30.0, which is right for
    the workflow steps calling it through `python -c` — they used to swallow a
    checksum refusal and exit 0. It is wrong for an interactive command that
    also installs the agent bundles: aborting would cost the user those over a
    binary they can install separately, and the refusal is printed either way.

    `cli.setup` imports `install_omnigraph` inside the function body, so the
    patch target is witan_core's own attribute rather than a module-level name
    in cli.
    """
    import witan_core

    from witan_code import cli

    calls: list[dict] = []
    monkeypatch.setattr(
        witan_core,
        "install_omnigraph",
        lambda dry_run, **kwargs: calls.append({"dry_run": dry_run, **kwargs}),
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    cli.setup(agent="claude", author="tester")

    assert calls == [{"dry_run": False, "strict": False}]
