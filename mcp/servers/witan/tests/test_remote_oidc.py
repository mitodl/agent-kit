"""witan's binding of the shared OIDC device-auth core (ADR 0005, path a).

The device-grant/cache/refresh mechanics are covered exhaustively in
witan-core (tests/test_remote_oidc.py). Here we pin only what witan's shim
binds on top: the ``~/.config/witan/tokens.json`` cache location (and its
``WITAN_TOKEN_CACHE`` override), the ``witan login`` re-auth hint, and that the
re-exported ``login``/``get_valid_token``/``logout`` API round-trips.
"""

from __future__ import annotations

import base64
import json
import stat
import time

import httpx
import pytest

from witan.config import RemoteConfig
from witan.remote import oidc


@pytest.fixture
def cfg():
    return RemoteConfig(
        url="https://witan.example.org/mcp",
        oidc_issuer="https://sso.example.org/realms/ol",
        oidc_client_id="witan-cli",
    )


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("WITAN_TOKEN_CACHE", str(tmp_path / "tokens.json"))


def _jwt(claims: dict) -> str:
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.sig"


def _seed_cache(cfg: RemoteConfig, entry: dict) -> None:
    """Write a cache entry directly, keyed the way the core keys it."""
    path = oidc._cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = f"{cfg.oidc_issuer}|{cfg.oidc_client_id}"
    path.write_text(json.dumps({key: entry}), encoding="utf-8")


_META = {
    # A real metadata document always echoes its own issuer, and
    # witan_core.remote.oidc.discover_endpoints now requires it to match the one
    # we asked for (RFC 9207 hardening). Keep in sync with the `cfg` fixture's
    # oidc_issuer above — these tests drive the core through witan's shim.
    "issuer": "https://sso.example.org/realms/ol",
    "device_authorization_endpoint": "https://sso.example.org/dev",
    "token_endpoint": "https://sso.example.org/token",
}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_default_cache_path_is_under_config_witan(monkeypatch):
    monkeypatch.delenv("WITAN_TOKEN_CACHE", raising=False)
    assert oidc._cache_path().as_posix().endswith(".config/witan/tokens.json")


def test_env_override_redirects_cache(tmp_path):
    # The autouse fixture points WITAN_TOKEN_CACHE at tmp_path.
    assert oidc._cache_path() == tmp_path / "tokens.json"


def test_login_round_trips_through_shim_and_caches(cfg, tmp_path):
    access = _jwt({"sub": "u-1", "preferred_username": "alice"})

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=_META)
        if str(req.url) == _META["device_authorization_endpoint"]:
            return httpx.Response(
                200, json={"device_code": "d", "user_code": "WXYZ", "expires_in": 300}
            )
        return httpx.Response(200, json={"access_token": access, "expires_in": 300})

    claims = oidc.login(
        cfg, on_prompt=lambda _d: None, client=_client(handler), sleep=lambda _s: None
    )
    assert claims["preferred_username"] == "alice"
    # Cached at the witan location, readable back, mode 0600.
    assert oidc.get_valid_token(cfg) == access
    assert stat.S_IMODE((tmp_path / "tokens.json").stat().st_mode) == 0o600
    # default_token_provider yields the same token per call.
    assert oidc.default_token_provider(cfg)() == access


def test_get_valid_token_without_login_mentions_witan_login(cfg):
    with pytest.raises(oidc.NeedsLogin, match="witan login"):
        oidc.get_valid_token(cfg)


def test_refresh_uses_cached_entry_at_witan_path(cfg):
    fresh = _jwt({"sub": "u", "preferred_username": "alice"})
    _seed_cache(
        cfg,
        {
            "access_token": _jwt({"sub": "u"}),
            "refresh_token": "r-old",
            "expires_at": time.time() - 100,
        },
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=_META)
        assert "grant_type=refresh_token" in req.content.decode()
        return httpx.Response(
            200,
            json={"access_token": fresh, "refresh_token": "r-new", "expires_in": 300},
        )

    assert oidc.get_valid_token(cfg, client=_client(handler)) == fresh


def test_logout_clears_cached_session(cfg):
    _seed_cache(cfg, {"access_token": "a", "expires_at": time.time() + 999})
    assert oidc.logout(cfg) is True
    assert oidc.logout(cfg) is False
