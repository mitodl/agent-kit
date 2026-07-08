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
    assert "agent-kit" in capsys.readouterr().out.lower()


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
    """Passing --scope project routes claude's mcp target to the
    project-scoped .mcp.json (relative to CWD, i.e. the repo root agent-kit is
    run from) instead of the manifest's own [options] scope = "global"."""
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
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
    assert (
        json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["witan"][
            "command"
        ]
        == "uvx"
    )


def test_apply_zero_arg_resolves_repo_local_manifest(tmp_path, monkeypatch, capsys):
    """No MANIFEST given, no global config file at all -> still resolves via
    a repo-local agent-config.toml at the repo root (O2 step 2), with no
    global config needed."""
    import subprocess

    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("AC_KIT_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    _write_manifest(
        repo,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )
    monkeypatch.chdir(repo)

    _run_ok(app, ["apply", "--platform", "claude", "--scope", "project"])

    out = capsys.readouterr().out
    assert "resolved manifest from repo-local manifest" in out
    assert (
        json.loads((repo / ".mcp.json").read_text())["mcpServers"]["witan"]["command"]
        == "uvx"
    )


def test_apply_zero_arg_falls_back_to_default_manifest_from_global_config(
    tmp_path, monkeypatch, capsys
):
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("AC_KIT_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

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
    config_dir = tmp_path / ".config" / "agent-config-kit"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(f'default_manifest = "{manifest}"\n')

    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    _run_ok(app, ["apply", "--platform", "claude"])

    out = capsys.readouterr().out
    assert "resolved manifest from default_manifest" in out
    assert (
        json.loads((cwd / ".mcp.json").read_text())["mcpServers"]["witan"]["command"]
        == "uvx"
    )


def test_apply_zero_arg_exits_2_with_no_manifest_and_no_config(
    tmp_path, monkeypatch, capsys
):
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("AC_KIT_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        app(["apply"])

    assert exc_info.value.code == 2
    assert "no MANIFEST given" in capsys.readouterr().out


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


def test_apply_prune_does_not_record_state_for_a_skipped_platform(
    tmp_path, monkeypatch
):
    """A platform whose target couldn't be parsed this run never actually got
    applied — recording its state anyway would let a *later* prune remove
    entries it never truly wrote. The prior state (if any) must survive
    untouched instead."""
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
    good_state = json.loads(state_file.read_text())
    assert good_state["platforms"]["claude"]["mcp_servers"] == ["witan"]

    (tmp_path / ".claude.json").write_text("[1, 2, 3]")  # now unparsable

    with pytest.raises(SystemExit):
        app(["apply", str(manifest), "--platform", "claude", "--prune"])

    state_after_skip = json.loads(state_file.read_text())
    assert state_after_skip["platforms"]["claude"]["mcp_servers"] == ["witan"]


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


def test_apply_with_profile_only_installs_selected_entries(tmp_path, monkeypatch):
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
        command = "other"

        [profiles.universal]
        mcp_servers = ["witan"]
        """,
    )

    _run_ok(
        app,
        ["apply", str(manifest), "--platform", "claude", "--profile", "universal"],
    )

    cfg = json.loads((tmp_path / ".claude.json").read_text())
    assert "witan" in cfg["mcpServers"]
    assert "other" not in cfg["mcpServers"]


def test_apply_profile_flag_overrides_manifest_default_profiles(tmp_path, monkeypatch):
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
        command = "other"

        [profiles.a]
        mcp_servers = ["witan"]

        [profiles.b]
        mcp_servers = ["other"]

        [options]
        default_profiles = ["a"]
        """,
    )

    _run_ok(app, ["apply", str(manifest), "--platform", "claude", "--profile", "b"])

    cfg = json.loads((tmp_path / ".claude.json").read_text())
    assert "other" in cfg["mcpServers"]
    assert "witan" not in cfg["mcpServers"]


def test_apply_no_profile_flag_uses_manifest_default_profiles(tmp_path, monkeypatch):
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
        command = "other"

        [profiles.a]
        mcp_servers = ["witan"]

        [options]
        default_profiles = ["a"]
        """,
    )

    _run_ok(app, ["apply", str(manifest), "--platform", "claude"])

    cfg = json.loads((tmp_path / ".claude.json").read_text())
    assert "witan" in cfg["mcpServers"]
    assert "other" not in cfg["mcpServers"]


def test_apply_unknown_profile_exits_2(tmp_path, monkeypatch, capsys):
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    manifest = _write_manifest(
        tmp_path,
        """
        [profiles.universal]
        """,
    )

    with pytest.raises(SystemExit) as exc_info:
        app(["apply", str(manifest), "--profile", "does-not-exist"])

    assert exc_info.value.code == 2
    assert "does-not-exist" in capsys.readouterr().out


def test_profiles_command_lists_profiles_with_resolved_counts(tmp_path, capsys):
    from agent_config_kit.cli import app

    manifest = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"

        [profiles.universal]
        mcp_servers = ["witan"]

        [profiles.frontend]
        inherits = ["universal"]
        """,
    )

    _run_ok(app, ["profiles", str(manifest)])

    out = capsys.readouterr().out
    assert "universal" in out
    assert "frontend" in out


def test_profiles_command_reports_no_profiles(tmp_path, capsys):
    from agent_config_kit.cli import app

    manifest = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )

    _run_ok(app, ["profiles", str(manifest)])

    assert "no profiles" in capsys.readouterr().out


def test_config_init_writes_all_commented_starter_at_default_location(
    tmp_path, monkeypatch
):
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("AC_KIT_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    _run_ok(app, ["config", "init"])

    config_path = tmp_path / ".config" / "agent-config-kit" / "config.toml"
    text = config_path.read_text()
    assert "# default_manifest" in text
    assert "# [[org]]" in text
    assert "# [[scope]]" in text

    from agent_config_kit.config import GlobalConfig, load_global_config

    assert load_global_config(config_path) == GlobalConfig()


def test_config_init_respects_explicit_config_flag(tmp_path):
    from agent_config_kit.cli import app

    config_path = tmp_path / "custom" / "config.toml"

    _run_ok(app, ["config", "init", "--config", str(config_path)])

    assert config_path.is_file()


def test_config_init_refuses_to_overwrite_without_force(tmp_path, capsys):
    from agent_config_kit.cli import app

    config_path = tmp_path / "config.toml"
    config_path.write_text("default_manifest = 'existing'\n")

    with pytest.raises(SystemExit) as exc_info:
        app(["config", "init", "--config", str(config_path)])

    assert exc_info.value.code == 1
    assert "already exists" in capsys.readouterr().out
    assert config_path.read_text() == "default_manifest = 'existing'\n"


def test_config_init_force_overwrites_existing_file(tmp_path):
    from agent_config_kit.cli import app

    config_path = tmp_path / "config.toml"
    config_path.write_text("default_manifest = 'existing'\n")

    _run_ok(app, ["config", "init", "--config", str(config_path), "--force"])

    assert "existing" not in config_path.read_text()


def test_config_init_wizard_writes_supplied_values_and_comments_out_the_rest(
    tmp_path, monkeypatch
):
    """The wizard fills in what the user answers and leaves the rest as the
    same commented-out example the non-interactive path writes."""
    import agent_config_kit.cli as cli_module
    from agent_config_kit.cli import app

    answers = iter(["~/dotfiles/agent-config.toml", "universal,frontend"])
    # add-org? -> yes; add-another-org? -> no; add-scope? -> no
    yes_no = iter([True, False, False])
    org_answers = iter(["mitodl", "https://cfg.mitodl.org/agent-config.toml"])

    monkeypatch.setattr(cli_module, "_ask", lambda prompt, **kw: next(answers))
    monkeypatch.setattr(cli_module, "_ask_yes_no", lambda prompt, **kw: next(yes_no))
    monkeypatch.setattr(cli_module, "_ask_required", lambda prompt: next(org_answers))
    monkeypatch.setattr(
        cli_module,
        "_ask_list",
        lambda prompt: next(answers).split(",") if "Default profiles" in prompt else [],
    )

    config_path = tmp_path / "config.toml"
    _run_ok(app, ["config", "init", "--config", str(config_path), "--wizard"])

    text = config_path.read_text()
    assert 'default_manifest = "~/dotfiles/agent-config.toml"' in text
    assert 'default_profiles = ["universal", "frontend"]' in text
    assert "[[org]]" in text
    assert 'name     = "mitodl"' in text
    assert 'manifest = "https://cfg.mitodl.org/agent-config.toml"' in text
    assert "# [[org]]" not in text  # real entry written, not the commented example
    assert "# [[scope]]" in text  # no scope entries added -> stays commented

    from agent_config_kit.config import load_global_config

    loaded = load_global_config(config_path)
    assert loaded.default_manifest == str(
        Path.home() / "dotfiles" / "agent-config.toml"
    )
    assert loaded.org[0].name == "mitodl"


def test_config_init_wizard_skips_everything_when_all_answers_are_blank(
    tmp_path, monkeypatch
):
    import agent_config_kit.cli as cli_module
    from agent_config_kit.cli import app

    monkeypatch.setattr(cli_module, "_ask", lambda prompt, **kw: "")
    monkeypatch.setattr(cli_module, "_ask_list", lambda prompt: [])
    monkeypatch.setattr(cli_module, "_ask_yes_no", lambda prompt, **kw: False)

    config_path = tmp_path / "config.toml"
    _run_ok(app, ["config", "init", "--config", str(config_path), "--wizard"])

    from agent_config_kit.config import GlobalConfig, load_global_config

    assert load_global_config(config_path) == GlobalConfig()
