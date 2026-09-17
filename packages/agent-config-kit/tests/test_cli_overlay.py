"""End-to-end CLI coverage for the config-overlay feature — see "Part A" of
``agent-config-kit-config-overlay-spec.md``. Complements the unit-level
tests in ``test_config.py`` (parsing), ``test_manifest.py``
(``load_overlay_bundle``/``apply_overlay``), and ``test_resolve.py``
(``resolve_overlay``/zero-arg wiring) with the actual CLI behavior a user
would see.
"""

import json
from pathlib import Path

import pytest

from agent_config_kit.cli import app


def _write_manifest(tmp_path: Path, text: str, name: str = "agent-config.toml") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    manifest = tmp_path / name
    manifest.write_text(text)
    return manifest


def _write_config(tmp_path: Path, text: str) -> Path:
    config_dir = tmp_path / ".config" / "agent-config-kit"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.toml"
    config_path.write_text(text)
    return config_path


def _hermetic(monkeypatch, tmp_path: Path) -> None:
    """Every test here relies on `Path.home()`-relative config.toml
    resolution — this repo's `testsupport.hermetic` (loaded via
    conftest.py) redirects XDG_CONFIG_HOME/AC_KIT_CONFIG at session scope
    for test isolation, which would otherwise silently override the
    per-test `Path.home()` mock and make every write to
    `tmp_path/.config/...` invisible to `resolve_config_path`."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("AC_KIT_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


def _run_ok(args: list[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        app(args)
    assert exc_info.value.code == 0


def test_apply_explicit_manifest_merges_in_global_overlay_by_default(
    tmp_path, monkeypatch
):
    """I7: config.toml's global overlay applies even to an explicit
    MANIFEST argument, on by default."""
    _hermetic(monkeypatch, tmp_path)
    _write_config(
        tmp_path,
        """
        [overlay.mcp_servers.memory]
        kind = "stdio"
        command = "npx"
        args = ["-y", "@modelcontextprotocol/server-memory"]
        """,
    )
    manifest = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )

    _run_ok(["apply", str(manifest), "--platform", "claude"])

    cfg = json.loads((tmp_path / ".claude.json").read_text())
    assert cfg["mcpServers"]["witan"]["command"] == "uvx"
    assert cfg["mcpServers"]["memory"]["command"] == "npx"


def test_apply_explicit_manifest_no_overlay_flag_skips_overlay(tmp_path, monkeypatch):
    _hermetic(monkeypatch, tmp_path)
    _write_config(
        tmp_path,
        """
        [overlay.mcp_servers.memory]
        kind = "stdio"
        command = "npx"
        """,
    )
    manifest = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )

    _run_ok(["apply", str(manifest), "--platform", "claude", "--no-overlay"])

    cfg = json.loads((tmp_path / ".claude.json").read_text())
    assert "witan" in cfg["mcpServers"]
    assert "memory" not in cfg["mcpServers"]


def test_apply_zero_arg_default_manifest_branch_merges_in_global_overlay(
    tmp_path, monkeypatch, capsys
):
    _hermetic(monkeypatch, tmp_path)

    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    manifest = _write_manifest(
        dotfiles,
        """
        [options]
        scope = "project"

        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )
    _write_config(
        tmp_path,
        f"""
        default_manifest = "{manifest}"

        [overlay.mcp_servers.memory]
        kind = "stdio"
        command = "npx"
        """,
    )

    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    _run_ok(["apply", "--platform", "claude"])

    out = capsys.readouterr().out
    assert "resolved manifest from default_manifest" in out
    assert "+ 1 inline entry from config.toml (global)" in out
    cfg = json.loads((cwd / ".mcp.json").read_text())
    assert cfg["mcpServers"]["witan"]["command"] == "uvx"
    assert cfg["mcpServers"]["memory"]["command"] == "npx"


def test_apply_zero_arg_org_match_merges_in_org_and_global_overlay(
    tmp_path, monkeypatch, capsys
):
    import subprocess

    _hermetic(monkeypatch, tmp_path)

    org_manifest = _write_manifest(
        tmp_path / "org",
        """
        [options]
        scope = "project"

        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )
    _write_config(
        tmp_path,
        f"""
        [overlay.mcp_servers.memory]
        kind = "stdio"
        command = "npx"

        [[org]]
        name     = "mitodl"
        manifest = "{org_manifest}"

        [org.overlay.mcp_servers.grafana]
        kind = "stdio"
        command = "grafana-mcp"
        """,
    )

    repo = tmp_path / "code" / "mit" / "myrepo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/mitodl/agent-kit.git"],
        cwd=repo,
        check=True,
    )
    monkeypatch.chdir(repo)

    _run_ok(["apply", "--platform", "claude"])

    out = capsys.readouterr().out
    assert "+ 2 inline entries from config.toml (org 'mitodl' + global)" in out
    cfg = json.loads((repo / ".mcp.json").read_text())
    assert set(cfg["mcpServers"]) == {"witan", "memory", "grafana"}


def test_apply_overlay_resolved_manifest_wins_key_collision(
    tmp_path, monkeypatch, capsys
):
    """I4: the resolved manifest's own entry wins over an overlay entry of
    the same name, not the other way around. The visibility line must
    reflect this too — an overlay entry that lost the collision
    contributed nothing, so it must not be counted as an addition (a prior
    bug counted the overlay's raw size instead of its effective
    contribution)."""
    _hermetic(monkeypatch, tmp_path)
    _write_config(
        tmp_path,
        """
        [overlay.mcp_servers.witan]
        kind = "stdio"
        command = "from-overlay"
        """,
    )
    manifest = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "from-manifest"
        """,
    )

    _run_ok(["apply", str(manifest), "--platform", "claude"])

    cfg = json.loads((tmp_path / ".claude.json").read_text())
    assert cfg["mcpServers"]["witan"]["command"] == "from-manifest"
    out = capsys.readouterr().out
    assert "+ 0 inline entries from config.toml (global)" in out


def test_apply_overlay_visibility_line_counts_only_effective_additions(
    tmp_path, monkeypatch, capsys
):
    """A mixed run (one overlay entry collides and loses, one is new) must
    count only the new one, not the overlay's raw size of two."""
    _hermetic(monkeypatch, tmp_path)
    _write_config(
        tmp_path,
        """
        [overlay.mcp_servers.witan]
        kind = "stdio"
        command = "from-overlay"

        [overlay.mcp_servers.memory]
        kind = "stdio"
        command = "npx"
        """,
    )
    manifest = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "from-manifest"
        """,
    )

    _run_ok(["apply", str(manifest), "--platform", "claude"])

    out = capsys.readouterr().out
    assert "+ 1 inline entry from config.toml (global)" in out
    cfg = json.loads((tmp_path / ".claude.json").read_text())
    assert cfg["mcpServers"]["witan"]["command"] == "from-manifest"
    assert cfg["mcpServers"]["memory"]["command"] == "npx"


def test_apply_overlay_entry_survives_profile_filter(tmp_path, monkeypatch):
    """O-OVERLAY-PROFILES: an overlay entry always applies, even under a
    --profile selection that doesn't reference it."""
    _hermetic(monkeypatch, tmp_path)
    _write_config(
        tmp_path,
        """
        [overlay.mcp_servers.memory]
        kind = "stdio"
        command = "npx"
        """,
    )
    manifest = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"

        [mcp_servers.other]
        kind = "stdio"
        command = "other"

        [profiles.universal]
        mcp_servers = ["witan"]
        """,
    )

    _run_ok(["apply", str(manifest), "--platform", "claude", "--profile", "universal"])

    cfg = json.loads((tmp_path / ".claude.json").read_text())
    assert "witan" in cfg["mcpServers"]
    assert "other" not in cfg["mcpServers"]
    assert "memory" in cfg["mcpServers"]


def test_validate_folds_overlay_entries_into_the_reported_bundle(tmp_path, monkeypatch):
    """O-OVERLAY-VALIDATE: an overlay entry not yet installed shows up as
    ordinary missing drift, same as any other manifest entry would."""
    _hermetic(monkeypatch, tmp_path)
    _write_config(
        tmp_path,
        """
        [overlay.mcp_servers.memory]
        kind = "stdio"
        command = "npx"
        """,
    )
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


def test_apply_explicit_manifest_invalid_config_toml_exits_cleanly(
    tmp_path, monkeypatch, capsys
):
    """With overlays on by default, an explicit MANIFEST now loads
    config.toml too — an invalid config.toml must fail the same clean,
    non-traceback way an invalid manifest already does, not escape as an
    uncaught ConfigError."""
    _hermetic(monkeypatch, tmp_path)
    config_dir = tmp_path / ".config" / "agent-config-kit"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text("this is not [valid toml")
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

    assert exc_info.value.code == 2
    assert "invalid TOML" in capsys.readouterr().out


def test_apply_zero_arg_invalid_config_toml_exits_cleanly(tmp_path, monkeypatch):
    """Same ConfigError-must-not-escape guarantee for zero-arg resolution,
    which already loaded config.toml before this feature and had the same
    gap."""
    _hermetic(monkeypatch, tmp_path)
    config_dir = tmp_path / ".config" / "agent-config-kit"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text("this is not [valid toml")
    cwd = tmp_path / "somewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    with pytest.raises(SystemExit) as exc_info:
        app(["apply", "--platform", "claude"])

    assert exc_info.value.code == 2


def test_apply_explicit_manifest_overlay_instructions_rejected_cleanly(
    tmp_path, monkeypatch
):
    _hermetic(monkeypatch, tmp_path)
    _write_config(
        tmp_path,
        """
        [overlay]
        instructions = "See AGENTS.md"
        """,
    )
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

    assert exc_info.value.code == 2
