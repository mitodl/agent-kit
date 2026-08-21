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


def _stub_installer(monkeypatch) -> list[dict]:
    """Stub the omnigraph fetch and record how it was called."""
    calls: list[dict] = []

    def fake_install(dry_run, **kwargs):
        calls.append({"dry_run": dry_run, **kwargs})

    monkeypatch.setattr("witan.cli.setup_cmd.install_omnigraph", fake_install)
    return calls


@pytest.fixture
def _no_network(monkeypatch):
    """Every setup() call fetches the omnigraph binary; keep it a no-op."""
    _stub_installer(monkeypatch)
    monkeypatch.setattr("witan.setup.install_default_config", lambda dry_run: None)
    monkeypatch.setattr(setup_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_setup_does_not_abort_on_a_refused_omnigraph_binary(tmp_path, monkeypatch):
    """★ `witan setup` asks for several unrelated things in one run.

    The installer raises by default as of witan-core 0.30.0, which is right for
    the workflow steps calling it through `python -c` — they used to swallow a
    checksum refusal and exit 0. It is wrong here: aborting would cost the user
    config.toml and their agent bundles over a binary they can install
    separately, and the refusal is printed either way. So this caller opts out,
    and asserting it does is the point — a `**kwargs` stub would accept either.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("witan.setup.install_default_config", lambda dry_run: None)
    monkeypatch.setattr(setup_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = _stub_installer(monkeypatch)

    setup_cmd.setup(dry_run=False)

    assert calls == [{"dry_run": False, "strict": False}]


def _install_fake_witan_code(monkeypatch, tmp_path) -> Path:
    """Register a minimal fake `witan_code` + `witan_code.setup` module tree.

    The stub bundle-builder honors the real ``binary`` kwarg (default
    "witan-code") the same way witan_code.setup.witan_code_bundle does, so
    these tests actually exercise that setup_cmd passes `binary="witan code"`
    rather than just asserting on a hardcoded stub value. It also includes an
    mcp_servers entry — since setup_cmd must NOT merge that in (witan serve
    already mounts witan-code's tools in-process) — to catch a regression if
    that guard is ever removed.
    """
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

    def _stub_bundle(_pkg_dir: Path, author: str, *, binary: str = "witan-code"):
        return RegistrationBundle(
            mcp_servers={"witan-code": StdioServer(command="uvx", args=["stub"])},
            hooks=[
                DeclarativeHook(
                    event=HookEvent.USER_PROMPT_SUBMIT,
                    command=f"{binary} inject-context",
                )
            ],
        )

    fake_setup.witan_code_bundle = _stub_bundle
    monkeypatch.setitem(sys.modules, "witan_code", fake_pkg)
    monkeypatch.setitem(sys.modules, "witan_code.setup", fake_setup)
    return pkg_dir


def test_setup_merges_witan_code_hooks_as_nested_subcommand(
    tmp_path, monkeypatch, _no_network
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(setup_cmd, "_witan_code_mounted", lambda: True)
    _install_fake_witan_code(monkeypatch, tmp_path)

    setup_cmd.setup(dry_run=False)

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    commands = {
        h["command"] for e in settings["hooks"]["UserPromptSubmit"] for h in e["hooks"]
    }
    assert "witan inject-context" in commands
    assert "witan code inject-context" in commands  # nested, not "witan-code …"
    assert "witan-code inject-context" not in commands


def test_setup_does_not_register_a_duplicate_mcp_server_when_merged(
    tmp_path, monkeypatch, _no_network
):
    """witan serve already mounts witan-code's tools in-process, so folding in
    witan-code's own standalone MCP entry would just start a second, redundant
    server process exposing the same code_* tools twice."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(setup_cmd, "_witan_code_mounted", lambda: True)
    _install_fake_witan_code(monkeypatch, tmp_path)

    setup_cmd.setup(dry_run=False)

    claude_json = json.loads((tmp_path / ".claude.json").read_text())
    assert "witan" in claude_json["mcpServers"]
    assert "witan-code" not in claude_json["mcpServers"]


def test_setup_skips_witan_code_when_not_importable(tmp_path, monkeypatch, _no_network):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delitem(sys.modules, "witan_code", raising=False)
    monkeypatch.delitem(sys.modules, "witan_code.setup", raising=False)

    setup_cmd.setup(dry_run=False)

    claude_json = json.loads((tmp_path / ".claude.json").read_text())
    assert "witan" in claude_json["mcpServers"]
    assert "witan-code" not in claude_json["mcpServers"]

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    commands = {
        h["command"] for e in settings["hooks"]["UserPromptSubmit"] for h in e["hooks"]
    }
    assert "witan code inject-context" not in commands


def test_warns_when_witan_code_not_mounted_in_the_path_binary(
    tmp_path, monkeypatch, _no_network, capsys
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(setup_cmd, "_witan_code_mounted", lambda: False)
    _install_fake_witan_code(monkeypatch, tmp_path)

    setup_cmd.setup(dry_run=False)

    out = capsys.readouterr().out
    assert "witan code" in out
    assert "isn't available" in out


def test_no_warning_when_witan_code_is_mounted(
    tmp_path, monkeypatch, _no_network, capsys
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(setup_cmd, "_witan_code_mounted", lambda: True)
    _install_fake_witan_code(monkeypatch, tmp_path)

    setup_cmd.setup(dry_run=False)

    assert "isn't available" not in capsys.readouterr().out


def test_no_subcommand_warning_when_witan_itself_is_missing(
    tmp_path, monkeypatch, capsys
):
    """Only the existing "witan not on PATH" warning should print in this
    case — not also the "witan code isn't available" one. _witan_code_mounted()
    returns False when witan is missing too, so without the `shutil.which
    ("witan")` guard this fired both warnings side by side, each recommending
    a different (redundant) `uv tool install` command."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _stub_installer(monkeypatch)
    monkeypatch.setattr("witan.setup.install_default_config", lambda dry_run: None)
    monkeypatch.setattr(setup_cmd.shutil, "which", lambda name: None)
    _install_fake_witan_code(monkeypatch, tmp_path)

    setup_cmd.setup(dry_run=False)

    out = capsys.readouterr().out
    assert "witan not on PATH" in out
    assert "isn't available" not in out  # the "witan code …" warning


def test_witan_code_mounted_false_when_witan_not_on_path(monkeypatch):
    monkeypatch.setattr(setup_cmd.shutil, "which", lambda name: None)
    assert setup_cmd._witan_code_mounted() is False


def test_witan_code_mounted_true_when_subcommand_help_succeeds(monkeypatch):
    import subprocess

    monkeypatch.setattr(setup_cmd.shutil, "which", lambda name: "/usr/bin/witan")
    monkeypatch.setattr(
        setup_cmd.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], returncode=0),
    )
    assert setup_cmd._witan_code_mounted() is True


def test_witan_code_mounted_false_when_subcommand_help_fails(monkeypatch):
    import subprocess

    monkeypatch.setattr(setup_cmd.shutil, "which", lambda name: "/usr/bin/witan")
    monkeypatch.setattr(
        setup_cmd.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], returncode=1),
    )
    assert setup_cmd._witan_code_mounted() is False
