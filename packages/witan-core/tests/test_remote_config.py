"""The shared remote-config resolver (ADR 0005, path a).

Both servers' ``load_remote_config()`` funnel into ``resolve_remote_config``
after picking their own target block, so the env > target > file precedence and
the "URL without an issuer is a hard error" rule are pinned once, here.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from witan_core.remote.config import RemoteConfig, resolve_remote_config
from witan_core.remote.oidc import DEFAULT_CACHE_PATH, cache_path


@dataclass
class _Target:
    name: str = "hosted"
    remote_url: str | None = None
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_audience: str | None = None


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for var in (
        "WITAN_REMOTE_URL",
        "WITAN_OIDC_ISSUER",
        "WITAN_OIDC_CLIENT_ID",
        "WITAN_OIDC_AUDIENCE",
    ):
        monkeypatch.delenv(var, raising=False)


def test_nothing_configured_is_none():
    assert resolve_remote_config({}, None) is None


def test_env_wins_over_target_and_file(monkeypatch):
    monkeypatch.setenv("WITAN_REMOTE_URL", "https://from-env/mcp")
    monkeypatch.setenv("WITAN_OIDC_ISSUER", "https://sso/realms/env")
    target = _Target(remote_url="https://from-target/mcp")
    file_cfg = {"remote_url": "https://from-file/mcp"}

    cfg = resolve_remote_config(file_cfg, target)
    assert cfg.url == "https://from-env/mcp"
    assert cfg.target_name == "hosted"


def test_target_wins_over_file():
    target = _Target(
        remote_url="https://from-target/mcp", oidc_issuer="https://sso/realms/t"
    )
    cfg = resolve_remote_config({"remote_url": "https://from-file/mcp"}, target)
    assert cfg.url == "https://from-target/mcp"
    assert cfg.oidc_issuer == "https://sso/realms/t"


def test_file_is_the_last_source():
    cfg = resolve_remote_config(
        {"remote_url": "https://from-file/mcp", "oidc_issuer": "https://sso/realms/f"},
        None,
    )
    assert cfg.url == "https://from-file/mcp"
    assert cfg.target_name is None


def test_client_id_defaults_to_the_shared_witan_cli():
    # The default is load-bearing: DeviceAuth keys its cache by
    # (issuer, client_id), so both CLIs sharing it means one login covers both.
    cfg = resolve_remote_config(
        {"remote_url": "https://x/mcp", "oidc_issuer": "https://sso/realms/x"}, None
    )
    assert cfg.oidc_client_id == "witan-cli"
    assert cfg.oidc_audience is None


def test_url_without_issuer_is_a_hard_error():
    with pytest.raises(ValueError, match="WITAN_OIDC_ISSUER"):
        resolve_remote_config({"remote_url": "https://x/mcp"}, None)


def test_remote_config_satisfies_the_oidc_endpoint_protocol():
    cfg = RemoteConfig(url="https://x/mcp", oidc_issuer="https://sso/realms/x")
    # OidcEndpoint is structural; DeviceAuth reads exactly these four.
    assert (cfg.url, cfg.oidc_issuer, cfg.oidc_client_id, cfg.oidc_audience) == (
        "https://x/mcp",
        "https://sso/realms/x",
        "witan-cli",
        None,
    )


def test_cache_path_defaults_under_config_witan(monkeypatch):
    monkeypatch.delenv("WITAN_TOKEN_CACHE", raising=False)
    assert cache_path() == DEFAULT_CACHE_PATH
    assert DEFAULT_CACHE_PATH.as_posix().endswith(".config/witan/tokens.json")


def test_cache_path_honors_the_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("WITAN_TOKEN_CACHE", str(tmp_path / "t.json"))
    assert cache_path() == tmp_path / "t.json"


def test_cache_path_expands_a_tilde_override(monkeypatch):
    # A shell expands ~ before the var is set, but a config file, Docker ENV,
    # or a systemd unit does not — without expansion that override would make a
    # directory literally named "~" under the cwd.
    monkeypatch.setenv("WITAN_TOKEN_CACHE", "~/somewhere/tokens.json")
    from pathlib import Path

    resolved = cache_path()
    assert "~" not in resolved.as_posix()
    assert resolved == Path.home() / "somewhere" / "tokens.json"
