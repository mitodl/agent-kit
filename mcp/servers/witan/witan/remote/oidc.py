"""witan's binding of the shared OIDC device-auth core (ADR 0005, path a).

The device-grant login, token cache, and refresh live in
:mod:`witan_core.remote.oidc`; this module binds the two witan-specific bits —
the token-cache location (``~/.config/witan/tokens.json``, overridable with
``WITAN_TOKEN_CACHE``) and the ``witan login`` hint in "please re-authenticate"
messages — and re-exports the flow under the names ``witan.cli`` already calls.

The CLI never *verifies* the JWT — the deployed server does that against
Keycloak's JWKS (ADR-0004). :func:`decode_claims` is display-only.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

import httpx2
from witan_core.remote.oidc import (
    DeviceAuth,
    NeedsLogin,
    RemoteAuthError,
    decode_claims,
    discover_endpoints,
)

from ..config import RemoteConfig

_DEFAULT_CACHE = Path.home() / ".config" / "witan" / "tokens.json"
_LOGIN_HINT = "witan login"

__all__ = [
    "NeedsLogin",
    "RemoteAuthError",
    "decode_claims",
    "default_token_provider",
    "discover_endpoints",
    "get_valid_token",
    "login",
    "logout",
]


def _cache_path() -> Path:
    override = os.environ.get("WITAN_TOKEN_CACHE")
    return Path(override) if override else _DEFAULT_CACHE


def _auth(cfg: RemoteConfig) -> DeviceAuth:
    return DeviceAuth(cfg, _cache_path(), login_hint=_LOGIN_HINT)


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


def default_token_provider(cfg: RemoteConfig) -> Callable[[], str]:
    """A zero-arg callable the proxy calls per request to get a fresh token."""
    return lambda: get_valid_token(cfg)
