"""Tests for the `witan setup` CLI command's optional witan-code merge.

`witan setup` only ever hard-imports witan's own bundle; folding in
witan-code's is an *optional* import (see setup_cmd.py's docstring) — these
tests inject a fake `witan_code` module into ``sys.modules`` to exercise both
branches without a real witan-code install as a test dependency.
"""

import json
import sys
import types
from pathlib import Path

import pytest

from witan.cli import setup_cmd


@pytest.fixture
def _no_network(monkeypatch):
    """Every setup() call fetches the omnigraph binary; keep it a no-op."""
    monkeypatch.setattr("witan.setup.install_omnigraph", lambda dry_run: None)
    monkeypatch.setattr("witan.setup.install_default_config", lambda dry_run: None)
    monkeypatch.setattr(setup_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")


def _install_fake_witan_code(monkeypatch, tmp_path) -> Path:
    """Register a minimal fake `witan_code` + `witan_code.setup` module tree."""
    from agent_config_kit import (
        DeclarativeHook,
        HookEvent,
        RegistrationBundle,
        StdioServer,
    )

    pkg_dir = tmp_path / "fake_witan_code"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").touch()

    fake_pkg = types.ModuleType("witan_code")
    fake_pkg.__file__ = str(pkg_dir / "__init__.py")
    fake_setup = types.ModuleType("witan_code.setup")

    def _stub_bundle(_pkg_dir: Path, author: str):
        return RegistrationBundle(
            mcp_servers={"witan-code": StdioServer(command="uvx", args=["stub"])},
            hooks=[
                DeclarativeHook(
                    event=HookEvent.USER_PROMPT_SUBMIT,
                    command="witan-code inject-context",
                )
            ],
        )

    fake_setup.witan_code_bundle = _stub_bundle
    monkeypatch.setitem(sys.modules, "witan_code", fake_pkg)
    monkeypatch.setitem(sys.modules, "witan_code.setup", fake_setup)
    return pkg_dir


def test_setup_merges_witan_code_bundle_when_importable(
    tmp_path, monkeypatch, _no_network
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _install_fake_witan_code(monkeypatch, tmp_path)

    setup_cmd.setup(dry_run=False)

    claude_json = json.loads((tmp_path / ".claude.json").read_text())
    assert "witan" in claude_json["mcpServers"]
    assert "witan-code" in claude_json["mcpServers"]

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    commands = {
        h["command"] for e in settings["hooks"]["UserPromptSubmit"] for h in e["hooks"]
    }
    assert "witan inject-context" in commands
    assert "witan-code inject-context" in commands


def test_setup_skips_witan_code_when_not_importable(tmp_path, monkeypatch, _no_network):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delitem(sys.modules, "witan_code", raising=False)
    monkeypatch.delitem(sys.modules, "witan_code.setup", raising=False)

    setup_cmd.setup(dry_run=False)

    claude_json = json.loads((tmp_path / ".claude.json").read_text())
    assert "witan" in claude_json["mcpServers"]
    assert "witan-code" not in claude_json["mcpServers"]
