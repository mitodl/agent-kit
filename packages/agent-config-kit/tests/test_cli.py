import importlib
import json
import sys
from pathlib import Path

import pytest


def _write_manifest(tmp_path: Path, text: str) -> Path:
    manifest = tmp_path / "agent-config.toml"
    manifest.write_text(text)
    return manifest


def _run_ok(app, args: list[str]) -> None:
    """cyclopts' default result_action calls ``sys.exit(0)`` even on a
    successful ``None``-returning command, so a clean run must be asserted
    via ``pytest.raises`` too."""
    with pytest.raises(SystemExit) as exc_info:
        app(args)
    assert exc_info.value.code == 0


def test_cli_help_smoke(capsys):
    from agent_config_kit.cli import app

    with pytest.raises(SystemExit) as exc_info:
        app(["--help"])

    assert exc_info.value.code == 0
    assert "ac-kit" in capsys.readouterr().out.lower()


def test_cli_without_extra_exits_with_friendly_message(monkeypatch, capsys):
    """Importing agent_config_kit.cli without cyclopts installed must fail
    fast with an actionable message, not a bare traceback."""
    monkeypatch.setitem(sys.modules, "cyclopts", None)
    sys.modules.pop("agent_config_kit.cli", None)

    try:
        with pytest.raises(SystemExit) as exc_info:
            importlib.import_module("agent_config_kit.cli")
        assert exc_info.value.code == 1
        assert "cli" in capsys.readouterr().err.lower()
    finally:
        sys.modules.pop("agent_config_kit.cli", None)
        monkeypatch.delitem(sys.modules, "cyclopts", raising=False)
        importlib.import_module("agent_config_kit.cli")


def test_apply_writes_mcp_server_for_explicit_platform(tmp_path, monkeypatch, capsys):
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    manifest = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        args = ["witan", "serve"]
        """,
    )

    _run_ok(app, ["apply", str(manifest), "--platform", "claude"])

    cfg = json.loads((tmp_path / ".claude.json").read_text())
    assert cfg["mcpServers"]["witan"]["command"] == "uvx"
    assert "claude" in capsys.readouterr().out


def test_apply_defaults_to_detected_platforms_when_none_given(
    tmp_path, monkeypatch, capsys
):
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    manifest = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )

    _run_ok(app, ["apply", str(manifest)])

    assert (tmp_path / ".claude.json").exists()
    assert not (tmp_path / ".pi").exists()


def test_apply_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    manifest = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )

    _run_ok(app, ["apply", str(manifest), "--platform", "claude", "--dry-run"])

    assert not (tmp_path / ".claude.json").exists()
    assert "planned" in capsys.readouterr().out.lower()


def test_apply_cli_platform_overrides_manifest_platforms(tmp_path, monkeypatch):
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".pi").mkdir()
    manifest = _write_manifest(
        tmp_path,
        """
        [options]
        platforms = ["pi"]

        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )

    _run_ok(app, ["apply", str(manifest), "--platform", "claude"])

    assert (tmp_path / ".claude.json").exists()
    assert not (tmp_path / ".pi" / "agent" / "mcp.json").exists()


def test_apply_unknown_platform_exits_2(tmp_path, monkeypatch, capsys):
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    manifest = _write_manifest(tmp_path, "")

    with pytest.raises(SystemExit) as exc_info:
        app(["apply", str(manifest), "--platform", "not-a-real-platform"])

    assert exc_info.value.code == 2
    assert "not-a-real-platform" in capsys.readouterr().out


def test_apply_bad_manifest_exits_2(tmp_path, capsys):
    from agent_config_kit.cli import app

    manifest = _write_manifest(tmp_path, "this is not [valid toml")

    with pytest.raises(SystemExit) as exc_info:
        app(["apply", str(manifest)])

    assert exc_info.value.code == 2
    assert "invalid TOML" in capsys.readouterr().out


def test_apply_exits_1_when_a_platform_skips_a_target(tmp_path, monkeypatch, capsys):
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude.json").write_text("[1, 2, 3]")
    manifest = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )

    with pytest.raises(SystemExit) as exc_info:
        app(["apply", str(manifest), "--platform", "claude"])

    assert exc_info.value.code == 1
    assert "could not parse" in capsys.readouterr().out


def test_apply_cli_scope_overrides_manifest_scope(tmp_path, monkeypatch):
    """Passing --scope project should be accepted even though the current
    registry only populates global ScopeTargets (apply() itself already
    no-ops when a platform has no target for that scope)."""
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    manifest = _write_manifest(
        tmp_path,
        """
        [options]
        scope = "global"

        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )

    _run_ok(app, ["apply", str(manifest), "--platform", "claude", "--scope", "project"])

    assert not (tmp_path / ".claude.json").exists()


def test_validate_exits_0_and_no_drift_when_already_applied(
    tmp_path, monkeypatch, capsys
):
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    manifest = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )
    _run_ok(app, ["apply", str(manifest), "--platform", "claude"])

    _run_ok(app, ["validate", str(manifest), "--platform", "claude"])

    assert "claude" in capsys.readouterr().out


def test_validate_exits_1_when_manifest_not_yet_applied(tmp_path, monkeypatch, capsys):
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    manifest = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )

    with pytest.raises(SystemExit) as exc_info:
        app(["validate", str(manifest), "--platform", "claude"])

    assert exc_info.value.code == 1
    assert "mcp_servers:witan" in capsys.readouterr().out


def test_validate_exits_1_on_unreadable_target(tmp_path, monkeypatch, capsys):
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude.json").write_text("[1, 2, 3]")
    manifest = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )

    with pytest.raises(SystemExit) as exc_info:
        app(["validate", str(manifest), "--platform", "claude"])

    assert exc_info.value.code == 1
    assert "claude" in capsys.readouterr().out


def test_validate_defaults_to_detected_platforms_when_none_given(
    tmp_path, monkeypatch, capsys
):
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    manifest = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )

    with pytest.raises(SystemExit) as exc_info:
        app(["validate", str(manifest)])

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "claude" in out
    assert "pi" not in out


def test_validate_bad_manifest_exits_2(tmp_path, capsys):
    from agent_config_kit.cli import app

    manifest = _write_manifest(tmp_path, "this is not [valid toml")

    with pytest.raises(SystemExit) as exc_info:
        app(["validate", str(manifest)])

    assert exc_info.value.code == 2
    assert "invalid TOML" in capsys.readouterr().out


def test_apply_prune_writes_state_file_next_to_manifest(tmp_path, monkeypatch):
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    manifest = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )

    _run_ok(app, ["apply", str(manifest), "--platform", "claude", "--prune"])

    state_file = tmp_path / "agent-config.toml.lock.json"
    assert state_file.exists()
    state = json.loads(state_file.read_text())
    assert state["platforms"]["claude"]["mcp_servers"] == ["witan"]


def test_apply_without_prune_never_writes_state_file(tmp_path, monkeypatch):
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    manifest = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )

    _run_ok(app, ["apply", str(manifest), "--platform", "claude"])

    assert not (tmp_path / "agent-config.toml.lock.json").exists()


def test_apply_prune_removes_server_dropped_from_manifest_on_second_run(
    tmp_path, monkeypatch
):
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    manifest = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"

        [mcp_servers.other]
        kind = "stdio"
        command = "echo"
        """,
    )
    _run_ok(app, ["apply", str(manifest), "--platform", "claude", "--prune"])
    assert json.loads((tmp_path / ".claude.json").read_text())["mcpServers"]

    manifest = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )
    _run_ok(app, ["apply", str(manifest), "--platform", "claude", "--prune"])

    cfg = json.loads((tmp_path / ".claude.json").read_text())["mcpServers"]
    assert "witan" in cfg
    assert "other" not in cfg


def test_apply_prune_dry_run_writes_neither_config_nor_state_file(
    tmp_path, monkeypatch
):
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    manifest = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )

    _run_ok(
        app, ["apply", str(manifest), "--platform", "claude", "--prune", "--dry-run"]
    )

    assert not (tmp_path / ".claude.json").exists()
    assert not (tmp_path / "agent-config.toml.lock.json").exists()


def test_apply_prune_state_file_flag_overrides_default_path(tmp_path, monkeypatch):
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    manifest = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )
    custom_state = tmp_path / "custom.state.json"

    _run_ok(
        app,
        [
            "apply",
            str(manifest),
            "--platform",
            "claude",
            "--prune",
            "--state-file",
            str(custom_state),
        ],
    )

    assert custom_state.exists()
    assert not (tmp_path / "agent-config.toml.lock.json").exists()


def test_apply_then_validate_then_prune_full_lifecycle(tmp_path, monkeypatch):
    """End-to-end: apply a two-server manifest, confirm validate sees no
    drift, drop one server from the manifest, confirm validate now flags it
    as still-installed drift (validate never removes anything), then
    apply --prune to actually remove it, and confirm validate is clean
    again — all against a fake $HOME, no real config touched."""
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    two_servers = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        args = ["witan", "serve"]

        [mcp_servers.scratch]
        kind = "stdio"
        command = "echo"
        """,
    )

    _run_ok(app, ["apply", str(two_servers), "--platform", "claude", "--prune"])
    _run_ok(app, ["validate", str(two_servers), "--platform", "claude"])

    one_server = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        args = ["witan", "serve"]
        """,
    )

    with pytest.raises(SystemExit) as exc_info:
        app(["validate", str(one_server), "--platform", "claude"])
    assert exc_info.value.code == 0  # "scratch" isn't in this manifest at all

    cfg = json.loads((tmp_path / ".claude.json").read_text())
    assert "scratch" in cfg["mcpServers"]  # validate never mutates

    _run_ok(app, ["apply", str(one_server), "--platform", "claude", "--prune"])

    cfg = json.loads((tmp_path / ".claude.json").read_text())
    assert "witan" in cfg["mcpServers"]
    assert "scratch" not in cfg["mcpServers"]

    _run_ok(app, ["validate", str(one_server), "--platform", "claude"])
