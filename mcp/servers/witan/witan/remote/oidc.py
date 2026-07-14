"""OIDC device-authorization-grant login for the ``witan`` CLI (ADR 0005).

The device grant (RFC 8628) is the standard flow for CLI tools: no client
secret, no local redirect listener, works over SSH. ``witan login`` prints a
URL + user code, the human approves in a browser, and the CLI polls the token
endpoint until it gets a JWT. Tokens are cached (mode ``0600``) and refreshed
transparently, so day-to-day ``witan …`` commands never re-prompt.

The CLI never *verifies* the JWT — the deployed server does that against
Keycloak's JWKS (ADR-0004). :func:`decode_claims` here is display-only.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Callable

import httpx

from ..config import RemoteConfig

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_EXPIRY_SKEW_S = 30
_DEFAULT_CACHE = Path.home() / ".config" / "witan" / "tokens.json"


class RemoteAuthError(Exception):
    """Any failure obtaining/using a token for the remote witan service."""


class NeedsLogin(RemoteAuthError):
    """No usable cached token and none obtainable without user interaction.

    Raised by :func:`get_valid_token` when there is no cached access token and
    no refresh token to renew one — the caller must run ``witan login``.
    """


def _cache_path() -> Path:
    override = os.environ.get("WITAN_TOKEN_CACHE")
    return Path(override) if override else _DEFAULT_CACHE


def _cache_key(cfg: RemoteConfig) -> str:
    """Key an entry by (issuer, client_id) so multiple deployments coexist."""
    return f"{cfg.oidc_issuer}|{cfg.oidc_client_id}"


def _load_cache() -> dict:
    path = _cache_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_cache(cache: dict) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write via a temp file then chmod+replace so the token is never briefly
    # world-readable between create and chmod.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def discover_endpoints(issuer: str, *, client: httpx.Client | None = None) -> dict:
    """Fetch the realm's OIDC metadata (device + token endpoints)."""
    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    owns = client is None
    client = client or httpx.Client(timeout=15)
    try:
        resp = client.get(url)
        resp.raise_for_status()
        meta = resp.json()
    except httpx.HTTPError as exc:
        raise RemoteAuthError(
            f"Could not fetch OIDC metadata from {url}: {exc}"
        ) from exc
    finally:
        if owns:
            client.close()
    for key in ("device_authorization_endpoint", "token_endpoint"):
        if key not in meta:
            raise RemoteAuthError(
                f"OIDC provider at {issuer} does not advertise {key!r} — the "
                "device authorization grant may not be enabled for this realm."
            )
    return meta


def decode_claims(access_token: str) -> dict:
    """Base64url-decode a JWT's payload for display. Does NOT verify anything."""
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore stripped padding
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, json.JSONDecodeError):
        return {}


def _store_token(cfg: RemoteConfig, token: dict) -> dict:
    now = time.time()
    entry = {
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token"),
        "expires_at": now + float(token.get("expires_in", 0)),
        "obtained_at": now,
    }
    cache = _load_cache()
    cache[_cache_key(cfg)] = entry
    _write_cache(cache)
    return entry


def login(
    cfg: RemoteConfig,
    *,
    on_prompt: Callable[[dict], None],
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Run the device-authorization grant end to end and cache the token.

    ``on_prompt`` is called once with the device-code response so the CLI can
    tell the user where to go; ``sleep`` is injectable so tests don't wait.
    Returns the decoded JWT claims of the freshly-minted access token.
    """
    owns = client is None
    client = client or httpx.Client(timeout=15)
    try:
        meta = discover_endpoints(cfg.oidc_issuer, client=client)
        req = {"client_id": cfg.oidc_client_id, "scope": "openid"}
        resp = client.post(meta["device_authorization_endpoint"], data=req)
        resp.raise_for_status()
        device = resp.json()
        on_prompt(device)

        interval = float(device.get("interval", 5))
        deadline = time.time() + float(device.get("expires_in", 300))
        poll = {
            "grant_type": DEVICE_GRANT,
            "device_code": device["device_code"],
            "client_id": cfg.oidc_client_id,
        }
        while time.time() < deadline:
            sleep(interval)
            tok = client.post(meta["token_endpoint"], data=poll)
            if tok.status_code == 200:
                entry = _store_token(cfg, tok.json())
                return decode_claims(entry["access_token"])
            err = (tok.json() or {}).get("error", "")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            raise RemoteAuthError(f"Device authorization failed: {err or tok.text!r}")
        raise RemoteAuthError("Device code expired before it was approved.")
    except httpx.HTTPError as exc:
        raise RemoteAuthError(f"Device authorization request failed: {exc}") from exc
    finally:
        if owns:
            client.close()


def _refresh(cfg: RemoteConfig, refresh_token: str, client: httpx.Client) -> dict:
    meta = discover_endpoints(cfg.oidc_issuer, client=client)
    resp = client.post(
        meta["token_endpoint"],
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": cfg.oidc_client_id,
        },
    )
    if resp.status_code != 200:
        raise NeedsLogin(
            "Refresh token rejected — run `witan login` to re-authenticate."
        )
    return _store_token(cfg, resp.json())


def get_valid_token(cfg: RemoteConfig, *, client: httpx.Client | None = None) -> str:
    """Return a currently-valid access token, refreshing if needed.

    Raises :class:`NeedsLogin` when there is no cached token, or the cached one
    has expired and cannot be refreshed.
    """
    entry = _load_cache().get(_cache_key(cfg))
    if not entry:
        raise NeedsLogin(f"Not logged in to {cfg.url} — run `witan login` first.")
    if entry["expires_at"] - time.time() > _EXPIRY_SKEW_S:
        return entry["access_token"]
    if not entry.get("refresh_token"):
        raise NeedsLogin("Session expired — run `witan login` to re-authenticate.")
    owns = client is None
    client = client or httpx.Client(timeout=15)
    try:
        refreshed = _refresh(cfg, entry["refresh_token"], client)
    finally:
        if owns:
            client.close()
    return refreshed["access_token"]


def logout(cfg: RemoteConfig) -> bool:
    """Drop the cached token for this deployment. Returns True if one existed."""
    cache = _load_cache()
    existed = cache.pop(_cache_key(cfg), None) is not None
    if existed:
        _write_cache(cache)
    return existed


def default_token_provider(cfg: RemoteConfig) -> Callable[[], str]:
    """A zero-arg callable the proxy calls per request to get a fresh token."""
    return lambda: get_valid_token(cfg)
