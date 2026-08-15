"""How long the LOGIN lasts, and what a failed renewal actually means.

tk-a-witan-login-lasts-5-minutes-of-token-and-dies--550dd4. Two separate gaps,
both of which made a healthy session look like a dead one:

* the IdP sends ``refresh_expires_in`` on every token response and this client
  discarded it, so nothing could answer "am I still logged in?" — ``whoami``
  could only report the 5-minute access token, which is not the number anybody
  wants and reads as though a re-login is imminent;
* every non-200 from the token endpoint became ``NeedsLogin``, so a realm
  restarting behind a proxy sent the user through a device flow and abandoned a
  refresh token that was fine.

Companion to test_remote_oidc.py, which owns the login/cache/skew behaviour.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass

import httpx2
import pytest

from witan_core.remote.oidc import DeviceAuth, NeedsLogin, RemoteAuthError


@dataclass(frozen=True)
class _Endpoint:
    url: str = "https://witan.example.org/mcp"
    oidc_issuer: str = "https://sso.example.org/realms/ol"
    oidc_client_id: str = "witan-cli"
    oidc_audience: str | None = None


_META = {
    "issuer": _Endpoint.oidc_issuer,
    "device_authorization_endpoint": "https://sso.example.org/dev",
    "token_endpoint": "https://sso.example.org/token",
}


def _jwt(claims: dict) -> str:
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.sig"


@pytest.fixture
def auth(tmp_path):
    return DeviceAuth(_Endpoint(), tmp_path / "tokens.json", login_hint="witan login")


def _client(handler) -> httpx2.Client:
    return httpx2.Client(transport=httpx2.MockTransport(handler))


def _expired_entry(auth, **extra) -> None:
    auth._write_cache(
        {
            auth._cache_key(): {
                "access_token": _jwt({"sub": "u"}),
                "refresh_token": "r-old",
                "expires_at": time.time() - 100,
                **extra,
            }
        }
    )


def _responds(status: int, **kwargs):
    def handler(req: httpx2.Request) -> httpx2.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx2.Response(200, json=_META)
        return httpx2.Response(status, **kwargs)

    return handler


def _seed_refresh(auth, response: dict) -> None:
    """Drive one real refresh so ``response`` goes through ``_store_token``."""
    _expired_entry(auth)
    auth.get_valid_token(client=_client(_responds(200, json=response)))


# ── the login's own clock ────────────────────────────────────────────────────


def test_session_life_reports_a_finite_refresh_expiry(auth):
    _seed_refresh(
        auth,
        {
            "access_token": _jwt({"sub": "u"}),
            "refresh_token": "r-new",
            "expires_in": 300,
            "refresh_expires_in": 1800,
        },
    )
    life = auth.session_life()
    assert life.refresh_state == "finite"
    assert 1750 < life.refresh_expires_at - time.time() <= 1800
    # The two clocks are genuinely different — which is the entire point.
    assert life.access_expires_at < life.refresh_expires_at


def test_a_refresh_expires_in_of_zero_means_never_not_already_expired(auth):
    """Keycloak sends 0 for an offline token. Reading it as ``now + 0`` would
    mark an eternal session dead the instant it was issued — and the naive
    version of this change would have done exactly that."""
    _seed_refresh(
        auth,
        {
            "access_token": _jwt({"sub": "u"}),
            "refresh_token": "r-new",
            "expires_in": 300,
            "refresh_expires_in": 0,
        },
    )
    life = auth.session_life()
    assert life.refresh_state == "never"
    assert life.refresh_expires_at is None


def test_an_idp_that_says_nothing_is_unknown_not_never(auth):
    """Silence is not a promise of immortality. Rendering it as one would tell
    a user their session is fine when nothing checked."""
    _seed_refresh(
        auth,
        {
            "access_token": _jwt({"sub": "u"}),
            "refresh_token": "r-new",
            "expires_in": 300,
        },
    )
    assert auth.session_life().refresh_state == "unknown"


def test_a_cache_entry_from_an_older_client_is_unknown(auth):
    """The key is simply absent. It must read as unknown rather than crashing
    or being reported as an expiry of zero."""
    auth._write_cache(
        {
            auth._cache_key(): {
                "access_token": _jwt({"sub": "u"}),
                "refresh_token": "r-old",
                "expires_at": time.time() + 300,
            }
        }
    )
    life = auth.session_life()
    assert life.refresh_state == "unknown"
    assert life.access_expires_at is not None


def test_session_life_on_a_never_logged_in_cache_does_not_raise(auth):
    assert auth.session_life() == (None, None, "unknown")


def test_session_life_does_not_refresh_the_thing_it_measures(auth):
    """It is a report. Renewing while measuring would change the answer by
    asking, and would spend a rotating refresh token to do it. Passing no
    client at all is the assertion: it must not need one."""
    _expired_entry(auth, refresh_expires_at=time.time() + 1800)
    before = auth._load_cache()[auth._cache_key()]["access_token"]

    life = auth.session_life()

    assert life.refresh_state == "finite"
    assert auth._load_cache()[auth._cache_key()]["access_token"] == before


# ── "could not renew" is not "you are logged out" ────────────────────────────


def test_a_5xx_from_the_token_endpoint_is_retryable_not_a_logout(auth):
    """A realm restarting behind a proxy used to end the session."""
    _expired_entry(auth)

    with pytest.raises(RemoteAuthError) as caught:
        auth.get_valid_token(client=_client(_responds(503, text="upstream sad")))

    assert not isinstance(caught.value, NeedsLogin)
    assert "503" in str(caught.value)
    assert "witan login" not in str(caught.value)
    # And the refresh token survives, so the retry has something to use.
    assert auth._load_cache()[auth._cache_key()]["refresh_token"] == "r-old"


def test_only_invalid_grant_ends_the_session(auth):
    """A 400 that is not invalid_grant is a client/config fault, not an expired
    login — re-authenticating would not fix it, so do not advise it."""
    _expired_entry(auth)

    with pytest.raises(RemoteAuthError) as caught:
        auth.get_valid_token(
            client=_client(_responds(400, json={"error": "invalid_client"}))
        )

    assert not isinstance(caught.value, NeedsLogin)
    assert "invalid_client" in str(caught.value)


def test_invalid_grant_still_ends_the_session(auth):
    """The one case that genuinely means "log in again" must keep saying so."""
    _expired_entry(auth)

    with pytest.raises(NeedsLogin) as caught:
        auth.get_valid_token(
            client=_client(_responds(400, json={"error": "invalid_grant"}))
        )

    assert "witan login" in str(caught.value)


def test_force_refresh_classifies_the_same_way(auth):
    """It shares ``_refresh``, and the 401-recovery path is the worst place to
    mistake a bad gateway for a dead session."""
    auth._write_cache(
        {
            auth._cache_key(): {
                "access_token": "rejected-token",
                "refresh_token": "r-old",
                "expires_at": time.time() + 300,
            }
        }
    )

    with pytest.raises(RemoteAuthError) as caught:
        auth.force_refresh("rejected-token", client=_client(_responds(502, text="bad")))

    assert not isinstance(caught.value, NeedsLogin)
