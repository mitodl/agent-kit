import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary not on PATH"
)


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
    out = capsys.readouterr().out
    assert "no MANIFEST given" in out
    # regression: `[[org]]`/`[[scope]]` must render literally, not get
    # silently eaten as invalid Rich markup tags (a real bug the first
    # version of this message had).
    assert "[[org]]" in out
    assert "[[scope]]" in out


def test_apply_zero_arg_scope_match_with_no_profiles_overrides_manifest_default(
    tmp_path, monkeypatch
):
    """A matching [[scope]] entry with no `profiles` set resolves to an
    empty profiles list, not `None` -- that's still a real override from the
    resolution source (spec §7.2: "profile is taken from the same source"),
    so it must install the whole manifest rather than falling back to the
    manifest's own [options] default_profiles."""
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
        default_profiles = ["a"]

        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"

        [mcp_servers.other]
        kind = "stdio"
        command = "other"

        [profiles.a]
        mcp_servers = ["witan"]
        """,
    )
    config_dir = tmp_path / ".config" / "agent-config-kit"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        f'[[scope]]\nmatch_prefix = "{tmp_path / "code"}"\nmanifest = "{manifest}"\n'
    )

    cwd = tmp_path / "code" / "myrepo"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)

    _run_ok(app, ["apply", "--platform", "claude"])

    cfg = json.loads((cwd / ".mcp.json").read_text())
    assert "witan" in cfg["mcpServers"]
    assert "other" in cfg["mcpServers"]


def test_apply_zero_arg_passes_cache_dir_through_to_resolution(tmp_path, monkeypatch):
    """--cache-dir must reach a remote manifest resolved from the global
    config too, not just remote skill/hook sources inside an
    already-local manifest."""
    import agent_config_kit.resolve as resolve_module
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("AC_KIT_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    local_manifest = _write_manifest(
        tmp_path,
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )
    config_dir = tmp_path / ".config" / "agent-config-kit"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        'default_manifest = "https://example.invalid/agent-config.toml"\n'
    )

    seen_cache_dirs = []

    def fake_fetch_remote(uri, cache_dir):
        seen_cache_dirs.append(cache_dir)
        return local_manifest

    monkeypatch.setattr(resolve_module, "fetch_remote", fake_fetch_remote)

    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    custom_cache_dir = tmp_path / "custom-cache"

    _run_ok(
        app,
        ["apply", "--platform", "claude", "--cache-dir", str(custom_cache_dir)],
    )

    assert seen_cache_dirs == [custom_cache_dir]


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


def _write_skill(repo: Path, rel_dir: str, *, name: str | None = None) -> Path:
    skill_dir = repo / rel_dir
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    frontmatter = f"---\nname: {name}\ndescription: test\n---\n" if name else ""
    skill_md.write_text(f"{frontmatter}# {rel_dir}\n")
    return skill_md


def test_manifest_init_writes_skills_table_from_frontmatter_names(tmp_path):
    from agent_config_kit.cli import app

    repo = tmp_path / "repo"
    _write_skill(
        repo, "skills/python/cyclopts-cli-scripts", name="cyclopts-cli-scripts"
    )
    _write_skill(repo, "skills/process/dependency-pruning", name="dependency-pruning")

    _run_ok(app, ["manifest", "init", str(repo)])

    manifest_path = repo / "agent-config.toml"
    text = manifest_path.read_text()
    assert "[skills]" in text
    assert (
        'cyclopts-cli-scripts = "skills/python/cyclopts-cli-scripts/SKILL.md"' in text
    )
    assert 'dependency-pruning = "skills/process/dependency-pruning/SKILL.md"' in text

    from agent_config_kit.manifest import load_manifest

    loaded = load_manifest(manifest_path)
    assert {s.name for s in loaded.bundle.skills} == {
        "cyclopts-cli-scripts",
        "dependency-pruning",
    }


def test_manifest_init_falls_back_to_directory_name_without_frontmatter(tmp_path):
    from agent_config_kit.cli import app

    repo = tmp_path / "repo"
    _write_skill(repo, "skills/no-frontmatter")

    _run_ok(app, ["manifest", "init", str(repo)])

    text = (repo / "agent-config.toml").read_text()
    assert 'no-frontmatter = "skills/no-frontmatter/SKILL.md"' in text


def test_manifest_init_skips_vendor_and_dot_directories(tmp_path):
    from agent_config_kit.cli import app

    repo = tmp_path / "repo"
    _write_skill(repo, "skills/real-skill", name="real-skill")
    _write_skill(repo, "node_modules/some-pkg/skills/fake", name="fake")
    _write_skill(repo, ".venv/lib/site-packages/fake2", name="fake2")
    _write_skill(repo, ".claude/worktrees/other/skills/fake3", name="fake3")

    _run_ok(app, ["manifest", "init", str(repo)])

    from agent_config_kit.manifest import load_manifest

    loaded = load_manifest(repo / "agent-config.toml")
    assert {s.name for s in loaded.bundle.skills} == {"real-skill"}


def test_manifest_init_exits_2_on_duplicate_derived_name(tmp_path, capsys):
    from agent_config_kit.cli import app

    repo = tmp_path / "repo"
    _write_skill(repo, "skills/a", name="dup")
    _write_skill(repo, "skills/b", name="dup")

    with pytest.raises(SystemExit) as exc_info:
        app(["manifest", "init", str(repo)])

    assert exc_info.value.code == 2
    assert "duplicate skill name" in capsys.readouterr().out
    assert not (repo / "agent-config.toml").is_file()


def test_manifest_init_exits_2_on_invalid_derived_name(tmp_path, capsys):
    from agent_config_kit.cli import app

    repo = tmp_path / "repo"
    _write_skill(repo, "skills/Not_Valid", name="Not_Valid")

    with pytest.raises(SystemExit) as exc_info:
        app(["manifest", "init", str(repo)])

    assert exc_info.value.code == 2
    assert "not a valid Agent Skills name" in capsys.readouterr().out


def test_manifest_init_refuses_to_overwrite_without_force(tmp_path, capsys):
    from agent_config_kit.cli import app

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "agent-config.toml").write_text("# existing\n")

    with pytest.raises(SystemExit) as exc_info:
        app(["manifest", "init", str(repo)])

    assert exc_info.value.code == 1
    assert "already exists" in capsys.readouterr().out
    assert (repo / "agent-config.toml").read_text() == "# existing\n"


def test_manifest_init_force_overwrites_existing_file(tmp_path):
    from agent_config_kit.cli import app

    repo = tmp_path / "repo"
    _write_skill(repo, "skills/real-skill", name="real-skill")
    (repo / "agent-config.toml").write_text("# existing\n")

    _run_ok(app, ["manifest", "init", str(repo), "--force"])

    assert "real-skill" in (repo / "agent-config.toml").read_text()


def test_manifest_init_reports_and_writes_empty_skills_table_when_none_found(
    tmp_path, capsys
):
    from agent_config_kit.cli import app

    repo = tmp_path / "repo"
    repo.mkdir()

    _run_ok(app, ["manifest", "init", str(repo)])

    assert "no SKILL.md files found" in capsys.readouterr().out
    assert "[skills]" in (repo / "agent-config.toml").read_text()


def test_manifest_init_writes_paths_relative_to_output_not_repo(tmp_path):
    """A skill path in the generated manifest must resolve via manifest.py's
    own M5 rule (relative to the *manifest file's* directory) — so when
    ``--output`` places the manifest outside ``repo``, the written path must
    still be correct relative to the output location, not ``repo``."""
    from agent_config_kit.cli import app

    repo = tmp_path / "repo"
    _write_skill(repo, "skills/real-skill", name="real-skill")
    output = tmp_path / "elsewhere" / "agent-config.toml"

    _run_ok(app, ["manifest", "init", str(repo), "--output", str(output)])

    from agent_config_kit.manifest import load_manifest

    loaded = load_manifest(output)
    [skill] = loaded.bundle.skills
    assert skill.name == "real-skill"
    assert skill.skill_md_path.resolve() == repo / "skills" / "real-skill" / "SKILL.md"


def test_manifest_init_defaults_to_git_repo_root(tmp_path, monkeypatch):
    import subprocess

    from agent_config_kit.cli import app

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _write_skill(repo, "skills/real-skill", name="real-skill")
    nested = repo / "some" / "nested" / "dir"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    _run_ok(app, ["manifest", "init"])

    manifest_path = repo / "agent-config.toml"
    assert manifest_path.is_file()
    assert "real-skill" in manifest_path.read_text()


def _init_git_manifest_repo(path: Path, *, subdirectory: str) -> str:
    """A git repo with a manifest at ``<subdirectory>/agent-config.toml``
    that references a skill by a path relative to *that* manifest — the
    fixture used to prove a ``git+`` ``MANIFEST`` URI clones the whole repo
    (not just the one manifest file), so the manifest's own relative
    ``skill_md_path`` still resolves against its location inside the
    checkout, same as a local manifest (spec M5). Returns the resulting
    ``git+file://...#subdirectory=...`` URI."""
    manifest_dir = path / subdirectory
    (manifest_dir / "skills" / "remote-skill").mkdir(parents=True)
    (manifest_dir / "skills" / "remote-skill" / "SKILL.md").write_text(
        "---\nname: remote-skill\ndescription: test\n---\n"
    )
    (manifest_dir / "agent-config.toml").write_text(
        '[skills]\nremote-skill = "skills/remote-skill/SKILL.md"\n'
    )
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "init"], cwd=path, check=True)
    return f"git+file://{path}#subdirectory={subdirectory}/agent-config.toml"


@requires_git
def test_apply_accepts_an_explicit_remote_git_manifest_uri(tmp_path, monkeypatch):
    """A `git+` URI passed directly as MANIFEST (not via the global config's
    default_manifest/[[org]]/[[scope]] indirection) must itself be fetched
    and applied — regression test for a bug where cyclopts' `Path` argument
    coercion collapsed the URI's `scheme://` into `scheme:/` before
    `_resolve_manifest_arg` ever saw it, so an explicit remote MANIFEST
    silently failed to parse as a URI at all."""
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    source = tmp_path / "source"
    uri = _init_git_manifest_repo(source, subdirectory="platform-eng")
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    monkeypatch.chdir(consumer)

    _run_ok(
        app,
        [
            "apply",
            uri,
            "--platform",
            "claude",
            "--scope",
            "project",
            "--cache-dir",
            str(tmp_path / "cache"),
        ],
    )

    assert (consumer / ".claude" / "skills" / "remote-skill" / "SKILL.md").is_file()


@requires_git
def test_validate_accepts_an_explicit_remote_git_manifest_uri(tmp_path, monkeypatch):
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    source = tmp_path / "source"
    uri = _init_git_manifest_repo(source, subdirectory="platform-eng")
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    monkeypatch.chdir(consumer)

    with pytest.raises(SystemExit) as exc_info:
        app(
            [
                "validate",
                uri,
                "--platform",
                "claude",
                "--scope",
                "project",
                "--cache-dir",
                str(tmp_path / "cache"),
            ]
        )

    # exit 1: the skill is missing on disk (never applied) -- proves the
    # remote manifest was actually fetched and read, not silently skipped.
    assert exc_info.value.code == 1


@requires_git
def test_profiles_accepts_an_explicit_remote_git_manifest_uri(tmp_path, monkeypatch):
    from agent_config_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    source = tmp_path / "source"
    uri = _init_git_manifest_repo(source, subdirectory="platform-eng")

    _run_ok(app, ["profiles", uri, "--cache-dir", str(tmp_path / "cache")])
