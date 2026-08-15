"""witan's binding of the shared OIDC device-auth core (ADR 0005, path a).

The device-grant login, token cache, and refresh live in
:mod:`witan_core.remote.oidc`; this module binds the one witan-specific bit —
the ``witan login`` hint in "please re-authenticate" messages — and re-exports
the flow under the names ``witan.cli`` already calls. The cache location
(``~/.config/witan/tokens.json``, overridable with ``WITAN_TOKEN_CACHE``) is
shared with witan-code, and entries are keyed by ``(issuer, client_id)``, so a
single login serves both CLIs against one deployment.

The CLI never *verifies* the JWT — the deployed server does that against
Keycloak's JWKS (ADR-0004). :func:`decode_claims` is display-only.
"""

from __future__ import annotations

import time
from typing import Callable

import httpx2
from witan_core.remote.oidc import (
    DeviceAuth,
    NeedsLogin,
    RemoteAuthError,
    SessionLife,
    cache_path,
    decode_claims,
    device_auth,
    discover_endpoints,
)

from ..config import RemoteConfig

_LOGIN_HINT = "witan login"

__all__ = [
    "NeedsLogin",
    "RemoteAuthError",
    "SessionLife",
    "cache_path",
    "decode_claims",
    "default_token_provider",
    "default_token_refresher",
    "discover_endpoints",
    "get_valid_token",
    "login",
    "logout",
    "session_life",
]


def _auth(cfg: RemoteConfig) -> DeviceAuth:
    return device_auth(cfg, login_hint=_LOGIN_HINT)


def login(
    cfg: RemoteConfig,
    *,
    on_prompt: Callable[[dict], None],
    client: httpx2.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Run the device-authorization grant end to end and cache the token."""
    return _auth(cfg).login(on_prompt=on_prompt, client=client, sleep=sleep)


def get_valid_token(cfg: RemoteConfig, *, client: httpx2.Client | None = None) -> str:
    """Return a currently-valid access token, refreshing if needed."""
    return _auth(cfg).get_valid_token(client=client)


def logout(cfg: RemoteConfig) -> bool:
    """Drop the cached token for this deployment. True if one existed."""
    return _auth(cfg).logout()


def session_life(cfg: RemoteConfig) -> SessionLife:
    """When the cached access token, and the login behind it, run out."""
    return _auth(cfg).session_life()


def default_token_provider(cfg: RemoteConfig) -> Callable[[], str]:
    """A zero-arg callable the proxy calls per request to get a fresh token."""
    return lambda: get_valid_token(cfg)


def default_token_refresher(cfg: RemoteConfig) -> Callable[[str], str]:
    """Force-mint a token, given the one the deployment rejected.

    Handed to the proxy alongside the provider so it can recover from a
    credential the deployment rejected while this client still believed it was
    good — see ``RemoteMCPProxy.__init__``. Separate from the provider because
    the provider is allowed to answer from cache, which on a rejected token is
    exactly the wrong answer.

    Takes the rejected token rather than re-reading it: under concurrent 401s
    the cache may already hold somebody else's fresh one, and refreshing that
    would spend a rotating refresh token for nothing.
    """
    return lambda rejected: _auth(cfg).force_refresh(rejected)
