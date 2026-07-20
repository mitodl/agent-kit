import pytest

from witan_core.config_file import load_toml


def test_load_toml_missing_file_returns_empty(tmp_path):
    assert load_toml(tmp_path / "nonexistent.toml") == {}


def test_load_toml_reads_default_path(tmp_path, monkeypatch):
    monkeypatch.delenv("WITAN_CONFIG", raising=False)
    p = tmp_path / "config.toml"
    p.write_text('agent = "pi"\n')
    assert load_toml(p) == {"agent": "pi"}


def test_load_toml_env_var_overrides_default_path(tmp_path, monkeypatch):
    default = tmp_path / "default.toml"
    default.write_text('agent = "should-not-be-read"\n')
    override = tmp_path / "override.toml"
    override.write_text('agent = "pi"\n')
    monkeypatch.setenv("WITAN_CONFIG", str(override))
    assert load_toml(default) == {"agent": "pi"}


def test_load_toml_env_var_expands_tilde(tmp_path, monkeypatch):
    """WITAN_CONFIG is often set in contexts that skip shell tilde-expansion
    (Docker/systemd Environment=, a CI env block) — a literal ~ must still
    resolve rather than fail with a not-found on the literal path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config_dir = tmp_path / ".config" / "witan"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text('agent = "pi"\n')
    monkeypatch.setenv("WITAN_CONFIG", "~/.config/witan/config.toml")
    assert load_toml(tmp_path / "unused.toml") == {"agent": "pi"}


def test_load_toml_malformed_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("WITAN_CONFIG", raising=False)
    bad = tmp_path / "bad.toml"
    bad.write_text("this is not [ valid toml !!!")
    with pytest.raises(ValueError, match="Failed to parse config file"):
        load_toml(bad)
