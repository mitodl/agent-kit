"""Integration test for witan's own registration bundle.

Generic install-mechanics behavior (dry-run no-op, additive merge, JSON
skip-not-crash, hook dedup, ...) is covered by agent-config-kit's own test
suite (``packages/agent-config-kit/tests/``) — this only asserts that
``witan_bundle()`` + ``apply("claude", ...)`` produces *witan's* MCP entry and
hook commands, i.e. that the wiring is correct.
"""

import json
import re
from pathlib import Path

import pytest
from agent_config_kit import apply

from witan import setup

_OMNIGRAPH_VERSION_RE = re.compile(r'_OMNIGRAPH_VERSION = "([^"]+)"')


def test_witan_bundle_registers_witan_mcp_server_and_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()

    bundle = setup.witan_bundle(pkg_dir, "tester")
    apply("claude", bundle)

    claude_json = json.loads((tmp_path / ".claude.json").read_text())
    entry = claude_json["mcpServers"]["witan"]
    assert entry["type"] == "stdio"
    assert entry["command"] == "uvx"
    assert entry["env"]["WITAN_AUTHOR"] == "tester"

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert any(
        h["command"] == "witan inject-context"
        for e in settings["hooks"]["UserPromptSubmit"]
        for h in e["hooks"]
    )
    assert any(
        h["command"] == "witan session-checkpoint"
        for e in settings["hooks"]["Stop"]
        for h in e["hooks"]
    )


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


def test_omnigraph_version_matches_witan_code():
    """witan and witan-code each fetch their own omnigraph binary at runtime
    (no build-time bundling, no cross-package import — see setup.py's
    docstring), so nothing at import time enforces they stay pinned to the
    same release. renovate.json's customManager is supposed to bump both in
    one PR, but a manual edit to just one copy (exactly what caused a prior
    schema-version-mismatch outage) wouldn't touch the other — this is the
    CI-level backstop for that."""
    witan_code_setup = (
        Path(__file__).parent.parent.parent / "witan-code" / "witan_code" / "setup.py"
    )
    if not witan_code_setup.exists():
        pytest.skip("witan-code not present in this checkout")

    other_version = _OMNIGRAPH_VERSION_RE.search(witan_code_setup.read_text())
    assert other_version is not None, (
        f"couldn't find _OMNIGRAPH_VERSION in {witan_code_setup}"
    )
    assert setup._OMNIGRAPH_VERSION == other_version.group(1)
