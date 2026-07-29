"""Device-code login, token cache, and refresh (witan_core.remote.oidc).

The transport-agnostic core of ADR-0005 path a. Each server binds its own
cache path + login hint (see witan-council's shim); here we drive DeviceAuth
directly against a tmp cache and an httpx.MockTransport.
"""

from __future__ import annotations

import base64
import json
import stat
import time
from dataclasses import dataclass

import httpx
import pytest

from witan_core.remote.oidc import (
    DeviceAuth,
    NeedsLogin,
    RemoteAuthError,
    decode_claims,
)


@dataclass(frozen=True)
class _Endpoint:
    url: str = "https://witan.example.org/mcp"
    oidc_issuer: str = "https://sso.example.org/realms/ol"
    oidc_client_id: str = "witan-cli"
    oidc_audience: str | None = None


@pytest.fixture
def cache_path(tmp_path):
    return tmp_path / "tokens.json"


@pytest.fixture
def auth(cache_path):
    return DeviceAuth(_Endpoint(), cache_path, login_hint="witan login")


def _jwt(claims: dict) -> str:
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.sig"


_META = {
    # A real metadata document always echoes its own issuer; discover_endpoints
    # now requires it to match the one we asked for (RFC 9207 hardening).
    "issuer": _Endpoint.oidc_issuer,
    "device_authorization_endpoint": "https://sso.example.org/dev",
    "token_endpoint": "https://sso.example.org/token",
}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_decode_claims_is_display_only():
    tok = _jwt({"sub": "abc", "preferred_username": "alice"})
    assert decode_claims(tok)["preferred_username"] == "alice"
    assert decode_claims("not-a-jwt") == {}


def test_login_polls_until_approved_then_caches(auth, cache_path):
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
    claims = auth.login(
        on_prompt=prompts.append,
        client=_client(handler),
        sleep=lambda _s: None,
    )
    assert claims["preferred_username"] == "alice"
    assert prompts and prompts[0]["user_code"] == "WXYZ"
    assert calls["n"] == 3

    # Cached, valid, and readable back without re-auth.
    assert auth.get_valid_token() == access
    # Cache file is not group/world accessible.
    mode = stat.S_IMODE(cache_path.stat().st_mode)
    assert mode == 0o600


def test_login_slow_down_backs_off(auth):
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

    auth.login(on_prompt=lambda _d: None, client=_client(handler), sleep=seen.append)
    # First sleep at base interval 5, second after slow_down bumps it to 10.
    assert seen == [5, 10]


def test_login_access_denied_raises(auth):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=_META)
        if str(req.url) == _META["device_authorization_endpoint"]:
            return httpx.Response(200, json={"device_code": "d", "user_code": "C"})
        return httpx.Response(400, json={"error": "access_denied"})

    with pytest.raises(RemoteAuthError, match="access_denied"):
        auth.login(
            on_prompt=lambda _d: None, client=_client(handler), sleep=lambda _s: None
        )


def test_non_json_metadata_raises_clean_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>proxy error</html>")

    from witan_core.remote.oidc import discover_endpoints

    with pytest.raises(RemoteAuthError, match="non-JSON"):
        discover_endpoints(_Endpoint().oidc_issuer, client=_client(handler))


def test_issuer_mismatch_is_refused():
    """An AS mix-up presents exactly this way: reachable metadata at the
    configured issuer's well-known path that points elsewhere."""
    from witan_core.remote.oidc import discover_endpoints

    evil = {**_META, "issuer": "https://attacker.example.net/realms/ol"}

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=evil)

    with pytest.raises(RemoteAuthError, match="does not match the configured issuer"):
        discover_endpoints(_Endpoint().oidc_issuer, client=_client(handler))


def test_missing_issuer_is_refused():
    from witan_core.remote.oidc import discover_endpoints

    bare = {k: v for k, v in _META.items() if k != "issuer"}

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=bare)

    with pytest.raises(RemoteAuthError, match="advertises issuer None"):
        discover_endpoints(_Endpoint().oidc_issuer, client=_client(handler))


def test_trailing_slash_is_not_a_mismatch():
    """The URL construction rstrips '/', so issuer comparison must too."""
    from witan_core.remote.oidc import discover_endpoints

    slashed = {**_META, "issuer": f"{_Endpoint.oidc_issuer}/"}

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=slashed)

    meta = discover_endpoints(f"{_Endpoint().oidc_issuer}/", client=_client(handler))
    assert meta["token_endpoint"] == _META["token_endpoint"]


def test_non_json_token_response_raises_not_crashes(auth):
    # A 200 with a non-JSON body on the token endpoint must surface as a clean
    # RemoteAuthError, not an unhandled JSONDecodeError.
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=_META)
        if str(req.url) == _META["device_authorization_endpoint"]:
            return httpx.Response(200, json={"device_code": "d", "user_code": "C"})
        return httpx.Response(200, text="not json")

    with pytest.raises(RemoteAuthError, match="non-JSON"):
        auth.login(
            on_prompt=lambda _d: None, client=_client(handler), sleep=lambda _s: None
        )


def test_non_json_error_body_falls_through_to_generic_error(auth):
    # A non-JSON *error* body must not crash the poll loop; err defaults to ""
    # and we raise the generic failure carrying the raw text.
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=_META)
        if str(req.url) == _META["device_authorization_endpoint"]:
            return httpx.Response(200, json={"device_code": "d", "user_code": "C"})
        return httpx.Response(400, text="<html>gateway timeout</html>")

    with pytest.raises(RemoteAuthError, match="Device authorization failed"):
        auth.login(
            on_prompt=lambda _d: None, client=_client(handler), sleep=lambda _s: None
        )


def test_audience_is_sent_when_configured(cache_path):
    auth = DeviceAuth(_Endpoint(oidc_audience="witan-api"), cache_path)
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

    auth.login(
        on_prompt=lambda _d: None, client=_client(handler), sleep=lambda _s: None
    )
    assert all(form.get("audience") == "witan-api" for form in seen)


def test_audience_absent_when_not_configured(auth):
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

    auth.login(
        on_prompt=lambda _d: None, client=_client(handler), sleep=lambda _s: None
    )
    assert all("audience" not in form for form in seen)


def test_get_valid_token_without_login_raises(auth):
    with pytest.raises(NeedsLogin):
        auth.get_valid_token()


def test_get_valid_token_refreshes_when_expired(auth):
    fresh = _jwt({"sub": "u", "preferred_username": "alice"})
    # Seed an expired entry with a refresh token.
    auth._write_cache(
        {
            auth._cache_key(): {
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

    assert auth.get_valid_token(client=_client(handler)) == fresh


def test_get_valid_token_refresh_rejected_needs_login(auth):
    auth._write_cache(
        {
            auth._cache_key(): {
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

    with pytest.raises(NeedsLogin):
        auth.get_valid_token(client=_client(handler))


def test_logout_clears_only_this_deployment(cache_path):
    # Two deployments share one cache file (keyed by issuer+client_id).
    one = DeviceAuth(_Endpoint(), cache_path)
    other = DeviceAuth(
        _Endpoint(url="x", oidc_issuer="https://other/realms/z"), cache_path
    )
    one._write_cache(
        {
            one._cache_key(): {"access_token": "a", "expires_at": time.time() + 999},
            other._cache_key(): {"access_token": "b", "expires_at": time.time() + 999},
        }
    )
    assert one.logout() is True
    assert one.logout() is False  # already gone
    # The other deployment's token survives.
    assert other.get_valid_token() == "b"


def test_login_hint_appears_in_needs_login_message(cache_path):
    auth = DeviceAuth(_Endpoint(), cache_path, login_hint="witan login")
    with pytest.raises(NeedsLogin, match="witan login"):
        auth.get_valid_token()
