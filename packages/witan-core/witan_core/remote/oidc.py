"""OIDC device-authorization-grant login + token cache (ADR-0005, path a).

The device grant (RFC 8628) is the standard flow for CLI tools: no client
secret, no local redirect listener, works over SSH. The caller prints a URL +
user code (via ``on_prompt``), the human approves in a browser, and
:meth:`DeviceAuth.login` polls the token endpoint until it gets a JWT. Tokens
are cached (mode ``0600``, keyed by issuer+client_id so multiple deployments
coexist) and refreshed transparently.

The CLI never *verifies* the JWT — the deployed server does that against the
IdP's JWKS. :func:`decode_claims` is display-only.

Server-specific policy is bound by the caller: the token-cache location and the
``login_hint`` woven into "run ``…``" messages are constructor args, so this
module is server-agnostic. Requires the ``remote`` extra (``httpx2``).
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Callable, Protocol

import httpx2

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_EXPIRY_SKEW_S = 30


DEFAULT_CACHE_PATH = Path.home() / ".config" / "witan" / "tokens.json"
"""Where both CLIs cache their tokens, overridable with ``WITAN_TOKEN_CACHE``.

Shared on purpose, next to the shared ``~/.config/witan/config.toml``: entries
are keyed by ``(issuer, client_id)``, so one ``witan login`` covers every CLI
pointing at the same deployment under the same client id.
"""


class OidcEndpoint(Protocol):
    """The client's view of a deployment the device grant authenticates to.

    Structural — any object with these attributes works (e.g.
    :class:`witan_core.remote.config.RemoteConfig`).
    """

    url: str
    oidc_issuer: str
    oidc_client_id: str
    oidc_audience: str | None


class RemoteAuthError(Exception):
    """Any failure obtaining/using a token for the remote service."""


class NeedsLogin(RemoteAuthError):
    """No usable cached token and none obtainable without user interaction.

    Raised by :meth:`DeviceAuth.get_valid_token` when there is no cached access
    token and no refresh token to renew one — the caller must log in again.
    """


def _json(resp: httpx2.Response, what: str) -> dict:
    """Parse a response body as JSON, or raise a clean RemoteAuthError.

    A provider (or an HTML proxy error in front of it) can return a 2xx with a
    non-JSON body; a bare ``resp.json()`` would then raise ``JSONDecodeError``
    and crash the CLI instead of surfacing a readable auth failure.
    """
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise RemoteAuthError(
            f"{what} returned a non-JSON response (HTTP {resp.status_code})."
        ) from exc


def _json_safe(resp: httpx2.Response) -> dict:
    """Best-effort dict parse for error bodies — never raises, {} on failure."""
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def discover_endpoints(issuer: str, *, client: httpx2.Client | None = None) -> dict:
    """Fetch the realm's OIDC metadata (device + token endpoints).

    The document's own ``issuer`` must match the one we asked for (RFC 8414 §3.3,
    and the mix-up defense RFC 9207 generalises). Without that check a hijacked or
    misconfigured metadata document can point the device grant at a *different*
    authorization server's endpoints while the client still believes it is talking
    to the configured issuer — so the user approves a code, and the token comes
    from an AS nobody vetted.
    """
    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    owns = client is None
    client = client or httpx2.Client(timeout=15)
    try:
        resp = client.get(url)
        resp.raise_for_status()
        meta = _json(resp, "OIDC metadata endpoint")
    except httpx2.HTTPError as exc:
        raise RemoteAuthError(
            f"Could not fetch OIDC metadata from {url}: {exc}"
        ) from exc
    finally:
        if owns:
            client.close()
    # A 200 whose body is a JSON array or scalar (an HTML-ish proxy page that
    # happens to parse, a misrouted endpoint) would make the .get() below raise
    # AttributeError. The endpoint-presence loop that used to run first tolerated
    # a list, so guard explicitly to keep the clean RemoteAuthError.
    if not isinstance(meta, dict):
        raise RemoteAuthError(
            f"OIDC metadata endpoint at {url} returned {type(meta).__name__}, "
            "not a JSON object."
        )
    advertised = meta.get("issuer")
    # Compare the way the URL above was built — a trailing slash is not a
    # different issuer, but anything else is.
    if not isinstance(advertised, str) or advertised.rstrip("/") != issuer.rstrip("/"):
        raise RemoteAuthError(
            f"OIDC metadata from {url} advertises issuer {advertised!r}, which does "
            f"not match the configured issuer {issuer!r}. Refusing to continue — "
            "this is how an authorization-server mix-up attack presents."
        )
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


class DeviceAuth:
    """Device-authorization grant + token cache for one deployment.

    Parameters
    ----------
    endpoint:
        The deployment's URL + OIDC coordinates (see :class:`OidcEndpoint`).
    cache_path:
        Where the 0600 token cache lives. The caller owns this location (naming
        is server-specific); entries are keyed by issuer+client_id so several
        deployments share one file safely.
    login_hint:
        The command a user runs to (re)authenticate, woven into ``NeedsLogin``
        messages (e.g. ``"witan login"``).
    """

    def __init__(
        self,
        endpoint: OidcEndpoint,
        cache_path: Path | str,
        *,
        login_hint: str = "log in",
    ) -> None:
        self._cfg = endpoint
        self._cache_path = Path(cache_path)
        self._login_hint = login_hint

    # ── token cache ────────────────────────────────────────────────────────
    def _cache_key(self) -> str:
        """Key an entry by (issuer, client_id) so multiple deployments coexist."""
        return f"{self._cfg.oidc_issuer}|{self._cfg.oidc_client_id}"

    def _load_cache(self) -> dict:
        try:
            raw = self._cache_path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _write_cache(self, cache: dict) -> None:
        path = self._cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create the temp file 0600 *at creation* (O_CREAT | mode), not
        # chmod-after, so the token is never even briefly group/world-readable,
        # then atomically replace. os.open honors the mode only when it creates
        # the file, so drop any stale temp first (a crashed prior write could
        # have left one with laxer perms that O_CREAT would silently reuse).
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(cache, indent=2))
        except BaseException:
            os.unlink(tmp)
            raise
        os.replace(tmp, path)

    def _store_token(self, token: dict) -> dict:
        if not token.get("access_token"):
            raise RemoteAuthError("Token response contained no access_token.")
        now = time.time()
        entry = {
            "access_token": token["access_token"],
            "refresh_token": token.get("refresh_token"),
            "expires_at": now + float(token.get("expires_in", 0)),
            "obtained_at": now,
        }
        cache = self._load_cache()
        cache[self._cache_key()] = entry
        self._write_cache(cache)
        return entry

    # ── request helpers ────────────────────────────────────────────────────
    def _auth_params(self) -> dict:
        """Request params common to the device-auth and token calls.

        Includes ``audience`` only when configured — Keycloak realms with an
        audience/resource mapper honor it to stamp the ``aud`` claim the
        deployment validates; realms without one ignore the extra param.
        """
        params = {"client_id": self._cfg.oidc_client_id}
        if self._cfg.oidc_audience:
            params["audience"] = self._cfg.oidc_audience
        return params

    # ── public flow ────────────────────────────────────────────────────────
    def login(
        self,
        *,
        on_prompt: Callable[[dict], None],
        client: httpx2.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> dict:
        """Run the device-authorization grant end to end and cache the token.

        ``on_prompt`` is called once with the device-code response so the CLI
        can tell the user where to go; ``sleep`` is injectable so tests don't
        wait. Returns the decoded JWT claims of the freshly-minted access token.
        """
        owns = client is None
        client = client or httpx2.Client(timeout=15)
        try:
            meta = discover_endpoints(self._cfg.oidc_issuer, client=client)
            req = {**self._auth_params(), "scope": "openid"}
            resp = client.post(meta["device_authorization_endpoint"], data=req)
            resp.raise_for_status()
            device = _json(resp, "device authorization endpoint")
            on_prompt(device)

            interval = float(device.get("interval", 5))
            deadline = time.time() + float(device.get("expires_in", 300))
            poll = {
                **self._auth_params(),
                "grant_type": DEVICE_GRANT,
                "device_code": device["device_code"],
            }
            while time.time() < deadline:
                sleep(interval)
                tok = client.post(meta["token_endpoint"], data=poll)
                if tok.status_code == 200:
                    entry = self._store_token(_json(tok, "token endpoint"))
                    return decode_claims(entry["access_token"])
                err = _json_safe(tok).get("error", "")
                if err == "authorization_pending":
                    continue
                if err == "slow_down":
                    interval += 5
                    continue
                raise RemoteAuthError(
                    f"Device authorization failed: {err or tok.text!r}"
                )
            raise RemoteAuthError("Device code expired before it was approved.")
        except httpx2.HTTPError as exc:
            raise RemoteAuthError(
                f"Device authorization request failed: {exc}"
            ) from exc
        finally:
            if owns:
                client.close()

    def _refresh(self, refresh_token: str, client: httpx2.Client) -> dict:
        meta = discover_endpoints(self._cfg.oidc_issuer, client=client)
        resp = client.post(
            meta["token_endpoint"],
            data={
                **self._auth_params(),
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        if resp.status_code != 200:
            raise NeedsLogin(
                f"Refresh token rejected — run `{self._login_hint}` to re-authenticate."
            )
        return self._store_token(_json(resp, "token endpoint"))

    def get_valid_token(self, *, client: httpx2.Client | None = None) -> str:
        """Return a currently-valid access token, refreshing if needed.

        Raises :class:`NeedsLogin` when there is no cached token, or the cached
        one has expired and cannot be refreshed.
        """
        entry = self._load_cache().get(self._cache_key())
        if not entry:
            raise NeedsLogin(
                f"Not logged in to {self._cfg.url} — run `{self._login_hint}` first."
            )
        if entry["expires_at"] - time.time() > _EXPIRY_SKEW_S:
            return entry["access_token"]
        if not entry.get("refresh_token"):
            raise NeedsLogin(
                f"Session expired — run `{self._login_hint}` to re-authenticate."
            )
        owns = client is None
        client = client or httpx2.Client(timeout=15)
        try:
            refreshed = self._refresh(entry["refresh_token"], client)
        finally:
            if owns:
                client.close()
        return refreshed["access_token"]

    def logout(self) -> bool:
        """Drop the cached token for this deployment. True if one existed."""
        cache = self._load_cache()
        existed = cache.pop(self._cache_key(), None) is not None
        if existed:
            self._write_cache(cache)
        return existed

    def token_provider(self) -> Callable[[], str]:
        """A zero-arg callable the proxy calls per request to get a fresh token."""
        return lambda: self.get_valid_token()


def cache_path() -> Path:
    """The token-cache location: ``$WITAN_TOKEN_CACHE`` or :data:`DEFAULT_CACHE_PATH`."""
    override = os.environ.get("WITAN_TOKEN_CACHE")
    return Path(override) if override else DEFAULT_CACHE_PATH


def device_auth(endpoint: OidcEndpoint, *, login_hint: str) -> DeviceAuth:
    """A :class:`DeviceAuth` on the shared token cache, hinting ``login_hint``."""
    return DeviceAuth(endpoint, cache_path(), login_hint=login_hint)
