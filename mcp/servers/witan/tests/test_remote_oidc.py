"""Device-code login, token cache, and refresh (ADR 0005, path a)."""

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


_META = {
    "device_authorization_endpoint": "https://sso.example.org/dev",
    "token_endpoint": "https://sso.example.org/token",
}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_decode_claims_is_display_only():
    tok = _jwt({"sub": "abc", "preferred_username": "alice"})
    assert oidc.decode_claims(tok)["preferred_username"] == "alice"
    assert oidc.decode_claims("not-a-jwt") == {}


def test_login_polls_until_approved_then_caches(cfg, tmp_path):
    access = _jwt({"sub": "u-1", "preferred_username": "alice"})
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=_META)
        if str(req.url) == _META["device_authorization_endpoint"]:
            return httpx.Response(
                200,
                json={
                    "device_code": "dev-code",
                    "user_code": "WXYZ",
                    "verification_uri": "https://sso.example.org/device",
                    "interval": 1,
                    "expires_in": 300,
                },
            )
        # token endpoint: pending twice, then success
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(400, json={"error": "authorization_pending"})
        return httpx.Response(
            200,
            json={"access_token": access, "refresh_token": "r-1", "expires_in": 300},
        )

    prompts = []
    claims = oidc.login(
        cfg,
        on_prompt=prompts.append,
        client=_client(handler),
        sleep=lambda _s: None,
    )
    assert claims["preferred_username"] == "alice"
    assert prompts and prompts[0]["user_code"] == "WXYZ"
    assert calls["n"] == 3

    # Cached, valid, and readable back without re-auth.
    assert oidc.get_valid_token(cfg) == access
    # Cache file is not group/world accessible.
    mode = stat.S_IMODE((tmp_path / "tokens.json").stat().st_mode)
    assert mode == 0o600


def test_login_slow_down_backs_off(cfg):
    access = _jwt({"sub": "u"})
    seen = []
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=_META)
        if str(req.url) == _META["device_authorization_endpoint"]:
            return httpx.Response(
                200, json={"device_code": "d", "user_code": "C", "interval": 5}
            )
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(400, json={"error": "slow_down"})
        return httpx.Response(200, json={"access_token": access, "expires_in": 60})

    oidc.login(
        cfg, on_prompt=lambda _d: None, client=_client(handler), sleep=seen.append
    )
    # First sleep at base interval 5, second after slow_down bumps it to 10.
    assert seen == [5, 10]


def test_login_access_denied_raises(cfg):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=_META)
        if str(req.url) == _META["device_authorization_endpoint"]:
            return httpx.Response(200, json={"device_code": "d", "user_code": "C"})
        return httpx.Response(400, json={"error": "access_denied"})

    with pytest.raises(oidc.RemoteAuthError, match="access_denied"):
        oidc.login(
            cfg,
            on_prompt=lambda _d: None,
            client=_client(handler),
            sleep=lambda _s: None,
        )


def test_non_json_metadata_raises_clean_error(cfg):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>proxy error</html>")

    with pytest.raises(oidc.RemoteAuthError, match="non-JSON"):
        oidc.discover_endpoints(cfg.oidc_issuer, client=_client(handler))


def test_non_json_token_response_raises_not_crashes(cfg):
    # A 200 with a non-JSON body on the token endpoint must surface as a clean
    # RemoteAuthError, not an unhandled JSONDecodeError.
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=_META)
        if str(req.url) == _META["device_authorization_endpoint"]:
            return httpx.Response(200, json={"device_code": "d", "user_code": "C"})
        return httpx.Response(200, text="not json")

    with pytest.raises(oidc.RemoteAuthError, match="non-JSON"):
        oidc.login(
            cfg,
            on_prompt=lambda _d: None,
            client=_client(handler),
            sleep=lambda _s: None,
        )


def test_non_json_error_body_falls_through_to_generic_error(cfg):
    # A non-JSON *error* body must not crash the poll loop; err defaults to ""
    # and we raise the generic failure carrying the raw text.
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=_META)
        if str(req.url) == _META["device_authorization_endpoint"]:
            return httpx.Response(200, json={"device_code": "d", "user_code": "C"})
        return httpx.Response(400, text="<html>gateway timeout</html>")

    with pytest.raises(oidc.RemoteAuthError, match="Device authorization failed"):
        oidc.login(
            cfg,
            on_prompt=lambda _d: None,
            client=_client(handler),
            sleep=lambda _s: None,
        )


def test_audience_is_sent_when_configured():
    cfg = RemoteConfig(
        url="https://witan.example.org/mcp",
        oidc_issuer="https://sso.example.org/realms/ol",
        oidc_audience="witan-api",
    )
    access = _jwt({"sub": "u"})
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=_META)
        # Capture the posted form for both device-auth and token calls.
        from urllib.parse import parse_qs

        seen.append({k: v[0] for k, v in parse_qs(req.content.decode()).items()})
        if str(req.url) == _META["device_authorization_endpoint"]:
            return httpx.Response(200, json={"device_code": "d", "user_code": "C"})
        return httpx.Response(200, json={"access_token": access, "expires_in": 60})

    oidc.login(
        cfg, on_prompt=lambda _d: None, client=_client(handler), sleep=lambda _s: None
    )
    assert all(form.get("audience") == "witan-api" for form in seen)


def test_audience_absent_when_not_configured(cfg):
    access = _jwt({"sub": "u"})
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=_META)
        from urllib.parse import parse_qs

        seen.append({k: v[0] for k, v in parse_qs(req.content.decode()).items()})
        if str(req.url) == _META["device_authorization_endpoint"]:
            return httpx.Response(200, json={"device_code": "d", "user_code": "C"})
        return httpx.Response(200, json={"access_token": access, "expires_in": 60})

    oidc.login(
        cfg, on_prompt=lambda _d: None, client=_client(handler), sleep=lambda _s: None
    )
    assert all("audience" not in form for form in seen)


def test_get_valid_token_without_login_raises(cfg):
    with pytest.raises(oidc.NeedsLogin):
        oidc.get_valid_token(cfg)


def test_get_valid_token_refreshes_when_expired(cfg):
    fresh = _jwt({"sub": "u", "preferred_username": "alice"})
    # Seed an expired entry with a refresh token.
    oidc._write_cache(
        {
            oidc._cache_key(cfg): {
                "access_token": _jwt({"sub": "u"}),
                "refresh_token": "r-old",
                "expires_at": time.time() - 100,
            }
        }
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=_META)
        # token endpoint — expect a refresh_token grant
        body = req.content.decode()
        assert "grant_type=refresh_token" in body
        return httpx.Response(
            200,
            json={"access_token": fresh, "refresh_token": "r-new", "expires_in": 300},
        )

    assert oidc.get_valid_token(cfg, client=_client(handler)) == fresh


def test_get_valid_token_refresh_rejected_needs_login(cfg):
    oidc._write_cache(
        {
            oidc._cache_key(cfg): {
                "access_token": _jwt({"sub": "u"}),
                "refresh_token": "r-old",
                "expires_at": time.time() - 100,
            }
        }
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=_META)
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(oidc.NeedsLogin):
        oidc.get_valid_token(cfg, client=_client(handler))


def test_logout_clears_only_this_deployment(cfg):
    other = RemoteConfig(url="x", oidc_issuer="https://other/realms/z")
    oidc._write_cache(
        {
            oidc._cache_key(cfg): {
                "access_token": "a",
                "expires_at": time.time() + 999,
            },
            oidc._cache_key(other): {
                "access_token": "b",
                "expires_at": time.time() + 999,
            },
        }
    )
    assert oidc.logout(cfg) is True
    assert oidc.logout(cfg) is False  # already gone
    # The other deployment's token survives.
    assert oidc.get_valid_token(other) == "b"
