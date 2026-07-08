from pathlib import Path

import pytest

from agent_config_kit.config import (
    ConfigError,
    default_config_path,
    load_global_config,
)
from agent_config_kit.models import Scope


def _write(tmp_path: Path, name: str, text: str) -> Path:
    config = tmp_path / name
    config.write_text(text)
    return config


def test_default_config_path_uses_xdg_config_home_when_set(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    assert (
        default_config_path() == tmp_path / "xdg" / "agent-config-kit" / "config.toml"
    )


def test_default_config_path_falls_back_to_home_dot_config(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    assert (
        default_config_path()
        == tmp_path / ".config" / "agent-config-kit" / "config.toml"
    )


def test_load_global_config_missing_file_returns_empty_config_not_an_error(tmp_path):
    result = load_global_config(tmp_path / "does-not-exist.toml")

    assert result.default_manifest is None
    assert result.default_profiles == []
    assert result.org == []
    assert result.scope == []


def test_load_global_config_uses_ac_kit_config_env_var_when_no_explicit_path(
    tmp_path, monkeypatch
):
    config_path = _write(
        tmp_path, "config.toml", 'default_manifest = "~/dotfiles/agent-config.toml"\n'
    )
    monkeypatch.setenv("AC_KIT_CONFIG", str(config_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    result = load_global_config()

    assert result.default_manifest == str(tmp_path / "dotfiles" / "agent-config.toml")


def test_load_global_config_explicit_path_wins_over_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_KIT_CONFIG", str(tmp_path / "wrong.toml"))
    config_path = _write(tmp_path, "right.toml", 'default_manifest = "explicit"\n')

    result = load_global_config(config_path)

    assert result.default_manifest == "explicit"


def test_load_global_config_valid_round_trip(tmp_path):
    config_path = _write(
        tmp_path,
        "config.toml",
        """
        default_manifest = "~/dotfiles/agent-config.toml"
        default_profiles = ["universal"]

        [[org]]
        name     = "mitodl"
        manifest = "https://cfg.mitodl.org/agent-config.toml"
        profiles = ["platform-eng"]

        [[scope]]
        match_prefix = "~/code/mit"
        manifest     = "https://cfg.mitodl.org/agent-config.toml"
        profiles     = ["platform-eng"]

        [[scope]]
        match_prefix = "~/code/personal"
        manifest     = "~/dotfiles/personal-agent-config.toml"
        profiles     = ["universal"]
        write_scope  = "project"
        """,
    )

    result = load_global_config(config_path)

    assert result.default_profiles == ["universal"]
    assert result.org[0].name == "mitodl"
    assert result.org[0].manifest == "https://cfg.mitodl.org/agent-config.toml"
    assert result.org[0].profiles == ["platform-eng"]
    assert result.scope[0].profiles == ["platform-eng"]
    assert result.scope[1].write_scope == Scope.PROJECT


def test_load_global_config_scope_entry_defaults_to_project_write_scope(tmp_path):
    config_path = _write(
        tmp_path,
        "config.toml",
        """
        [[scope]]
        match_prefix = "~/code/mit"
        manifest     = "~/dotfiles/agent-config.toml"
        """,
    )

    result = load_global_config(config_path)

    assert result.scope[0].write_scope == Scope.PROJECT


def test_load_global_config_expands_tilde_in_local_manifest_paths(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    config_path = _write(
        tmp_path,
        "config.toml",
        """
        [[org]]
        name     = "my-personal-gh"
        manifest = "~/dotfiles/personal-agent-config.toml"
        """,
    )

    result = load_global_config(config_path)

    assert result.org[0].manifest == str(
        tmp_path / "dotfiles" / "personal-agent-config.toml"
    )


def test_load_global_config_leaves_remote_manifest_uris_unexpanded(tmp_path):
    config_path = _write(
        tmp_path,
        "config.toml",
        """
        [[org]]
        name     = "mitodl"
        manifest = "https://cfg.mitodl.org/agent-config.toml"
        """,
    )

    result = load_global_config(config_path)

    assert result.org[0].manifest == "https://cfg.mitodl.org/agent-config.toml"


def test_load_global_config_expands_tilde_in_scope_match_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config_path = _write(
        tmp_path,
        "config.toml",
        """
        [[scope]]
        match_prefix = "~/code/mit"
        manifest     = "~/dotfiles/agent-config.toml"
        """,
    )

    result = load_global_config(config_path)

    assert result.scope[0].match_prefix == str(tmp_path / "code" / "mit")


def test_load_global_config_invalid_toml_raises_config_error(tmp_path):
    config_path = _write(tmp_path, "config.toml", "this is not [valid toml")

    with pytest.raises(ConfigError, match="invalid TOML"):
        load_global_config(config_path)


def test_load_global_config_unknown_top_level_key_raises_config_error(tmp_path):
    config_path = _write(tmp_path, "config.toml", 'defualt_manifest = "typo"\n')

    with pytest.raises(ConfigError, match="defualt_manifest"):
        load_global_config(config_path)


def test_load_global_config_org_missing_required_field_raises_config_error(tmp_path):
    config_path = _write(
        tmp_path,
        "config.toml",
        """
        [[org]]
        name = "mitodl"
        """,
    )

    with pytest.raises(ConfigError, match="manifest"):
        load_global_config(config_path)


def test_load_global_config_unreadable_file_raises_config_error(tmp_path, monkeypatch):
    config_path = _write(tmp_path, "config.toml", 'default_manifest = "x"\n')

    def _raise(self, encoding=None):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", _raise)

    with pytest.raises(ConfigError, match="could not read"):
        load_global_config(config_path)
