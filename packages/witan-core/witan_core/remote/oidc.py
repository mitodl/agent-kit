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
import fcntl
import json
import logging
import math
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, NamedTuple, Protocol

import httpx2

_log = logging.getLogger(__name__)

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# Scopes asked for at login, and what to settle for when the realm says no.
# `offline_access` is what decouples the refresh token's life from the
# interactive SSO session — see `_request_device_code` for why that is needed,
# what it widens, and why the fallback exists rather than a hard failure.
_LOGIN_SCOPE = "openid offline_access"
_FALLBACK_LOGIN_SCOPE = "openid"

# The only status on which a scope refusal is retried. RFC 6749 §5.2 puts
# `invalid_scope` at 400; anything else carrying that body is a different
# failure and must propagate.
_SCOPE_REJECTED_STATUS = 400

# How much life a cached access token must have left to be handed to a caller.
#
# ★ THIS IS A BOUND ON THE SLOWEST CALL THE TOKEN WILL BE USED FOR, not a
# round-trip allowance. `default_token_provider` is consulted PER REQUEST and
# returns any token with more than this left — so a skew of 30s hands back a
# token with 31 seconds of life for a call that may take 45, and the token dies
# mid-flight. Measured against the CI deployment 2026-08-13, one `memory_store`
# took 3-51s under a 24-writer burst (p95 ~44s). 30s was sized for a call that
# takes milliseconds; this service does not have one.
#
# The default therefore clears the observed maximum with headroom. Configurable
# because it is the same kind of number as WITAN_REMOTE_CALL_BUDGET_SECONDS —
# derived from one deployment's measured write cost, and due to move when that
# cost does (tk-upstream-omnigraph-a-single-row-insert-costs-a-f-eeeae3).
EXPIRY_SKEW_ENV_VAR = "WITAN_OIDC_EXPIRY_SKEW_SECONDS"
_EXPIRY_SKEW_S = 90

# ★ AND A CEILING, BECAUSE A SKEW LONGER THAN THE TOKEN IS A REFRESH STORM.
# If the IdP issues tokens shorter than the skew, no cached entry is ever
# "usable", every request refreshes, and a fleet of agents stampedes the token
# endpoint — precisely the failure the cache lock below exists to prevent
# (tk-concurrent-agents-stampede-the-oidc-token-refres-677984). Rather than let
# a misconfiguration or a short-token realm turn every call into a refresh, the
# effective skew is capped at half the token's own lifetime: a short token then
# still gets used for half its life instead of none of it.
_MAX_SKEW_FRACTION = 0.5


DEFAULT_CACHE_PATH = Path.home() / ".config" / "witan" / "tokens.json"
"""Where both CLIs cache their tokens, overridable with ``WITAN_TOKEN_CACHE``.

Shared on purpose, next to the shared ``~/.config/witan/config.toml``: entries
are keyed by ``(issuer, client_id)``, so one ``witan login`` covers every CLI
pointing at the same deployment under the same client id.
"""


_cache_lock_depth: dict[tuple[int, str], int] = {}
_cache_lock_guard = threading.Lock()


@contextmanager
def _cache_lock(cache_path: Path) -> Iterator[None]:
    """Hold ``<cache>.lock`` exclusively for the block, re-entrant per thread.

    The token cache is a *local* file, so an advisory ``flock`` genuinely
    coordinates every process that shares it — unlike the graph store, where
    the same pattern is skipped for remote stores because it cannot span hosts.

    Re-entrancy is not optional: ``flock`` conflicts between two file
    descriptors even within one process, so the nested
    ``get_valid_token`` → ``_refresh`` → ``_store_token`` path would deadlock
    against itself without the depth count. Same shape as
    ``witan_core.omnigraph.acquire_store_flock``.
    """
    lock_path = cache_path.with_name(cache_path.name + ".lock")
    key = (threading.get_ident(), str(lock_path))
    with _cache_lock_guard:
        depth = _cache_lock_depth.get(key, 0)
        if depth:
            _cache_lock_depth[key] = depth + 1
    if depth:
        try:
            yield
        finally:
            with _cache_lock_guard:
                _cache_lock_depth[key] -= 1
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")  # noqa: SIM115 — closed in the finally below
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        with _cache_lock_guard:
            _cache_lock_depth[key] = 1
        try:
            yield
        finally:
            with _cache_lock_guard:
                _cache_lock_depth.pop(key, None)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


class OidcEndpoint(Protocol):
    """The client's view of a deployment the device grant authenticates to.

    Structural — any object with these attributes works (e.g.
    :class:`witan_core.remote.config.RemoteConfig`).
    """

    url: str
    oidc_issuer: str
    oidc_client_id: str
    oidc_audience: str | None


def _env_seconds(name: str, default: float) -> float:
    """``name`` from the environment as a non-negative, finite number of seconds.

    A BAD VALUE FALLS BACK AND SAYS SO, rather than raising. This is read on the
    path to every authenticated request: a typo in a deployment env var must not
    turn every call into a crash, which is strictly worse than the mis-sizing it
    was meant to correct. Same rule, and the same reasoning, as
    ``witan_core.omnigraph._env_override``.

    ``nan``/``inf`` are refused explicitly — ``float()`` accepts both and ``nan``
    compares False against every bound, so a negative-only check waves them
    through and the comparison in ``_usable`` silently becomes always-false.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        _log.warning("%s=%r is not a number; using %r", name, raw, default)
        return default
    if not math.isfinite(value) or value < 0:
        _log.warning("%s=%r is not a usable duration; using %r", name, raw, default)
        return default
    return value


class RemoteAuthError(Exception):
    """Any failure obtaining/using a token for the remote service."""


class NeedsLogin(RemoteAuthError):
    """No usable cached token and none obtainable without user interaction.

    Raised by :meth:`DeviceAuth.get_valid_token` when there is no cached access
    token and no refresh token to renew one — the caller must log in again.

    ★ RESERVED FOR SESSIONS THAT ARE ACTUALLY OVER. Raising it for a token
    endpoint that merely could not be reached sends the user through a device
    flow they did not need, and abandons a refresh token that was fine. The
    IdP distinguishes the two and so must this: see ``_DEAD_GRANT_ERRORS``.
    """


#: OAuth 2 error codes that mean the refresh token itself is finished —
#: expired, revoked, or already spent by a rotating IdP (RFC 6749 §5.2). These
#: are the only answers a user can do anything about by logging in again, and
#: so the only ones worth a `NeedsLogin`.
_DEAD_GRANT_ERRORS = frozenset({"invalid_grant"})

#: HTTP statuses where trying the same request again can plausibly work: the
#: IdP is up but unhappy right now, or something between us and it is.
#:
#: ★ "NOT invalid_grant" IS NOT THE SAME AS "RETRYABLE", and conflating them
#: promises a recovery that cannot happen. `invalid_client` and
#: `unsupported_grant_type` are persistent configuration faults — the request
#: is malformed or the client is not what the realm thinks it is, so repeating
#: it verbatim fails identically forever. The fact worth keeping in BOTH cases
#: is that the refresh token was not rejected, so re-authenticating is not the
#: answer; what differs is whether waiting helps.
_RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class SessionLife(NamedTuple):
    """How long the access token and the login behind it have left.

    Two different clocks, and only the second one answers "do I have to log in
    again?". ``refresh_state`` says how much is actually known about it:

    ``"finite"``  ``refresh_expires_at`` is a real timestamp.
    ``"never"``   the refresh token does not expire (an offline token).
    ``"unknown"`` the IdP did not say, or this entry predates the client
                  storing it. NOT the same as "never" — do not render it as
                  reassurance.

    ★ ``renewable`` IS A SEPARATE QUESTION FROM ``refresh_state``, and the two
    were briefly conflated. A token response with no ``refresh_token`` at all
    is accepted and cached — ``_store_token`` allows it — and such a session
    cannot be renewed by anything: ``get_valid_token`` raises ``NeedsLogin``
    the moment the access token lapses. It lands in ``refresh_state ==
    "unknown"`` for want of anywhere else to go, which is indistinguishable
    from "the IdP was merely silent about the lifetime". Without this flag a
    caller cannot tell the two apart, and reporting "renews automatically" for
    the first is a promise nothing can keep.
    """

    access_expires_at: float | None
    refresh_expires_at: float | None
    refresh_state: str
    renewable: bool


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
        # then atomically replace.
        #
        # The temp name is unique per writer. It used to be a fixed
        # ``tokens.json.tmp`` preceded by an unlink, which meant concurrent
        # writers deleted each other's in-flight temp: 58-83% of simultaneous
        # refreshes died on FileExistsError (O_EXCL lost the race) or
        # FileNotFoundError (os.replace after someone else's unlink), and each
        # failure was a token refresh that never landed. A unique name cannot
        # collide, so O_EXCL still guarantees we created — and therefore own
        # the mode of — the file we are about to write.
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
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
        # ★ HOW LONG THE *LOGIN* LASTS, which is not `expires_at`. The access
        # token is minutes; the refresh token is what decides whether the user
        # has to run the device flow again, and the IdP has been telling us all
        # along in `refresh_expires_in` — we threw it away. Without it the
        # client cannot answer "am I still logged in?", cannot warn before a
        # session lapses, and `whoami` can only report the short number, which
        # reads alarmingly and is not the one anybody wants.
        #
        # THREE STATES, and collapsing them loses the interesting one.
        # `refresh_expires_in: 0` is Keycloak for "this refresh token does not
        # expire" (what `offline_access` mints) — the opposite of "expired", so
        # a naive `now + 0` would mark an eternal session as already dead.
        # Absent entirely means the IdP did not say; that is not the same as
        # "never", and an entry written by an older client has neither key.
        # `session_life` is the one place that reads this back, so no caller
        # has to know the convention.
        refresh_expires_in = token.get("refresh_expires_in")
        if token.get("refresh_token") and refresh_expires_in is not None:
            seconds = float(refresh_expires_in)
            entry["refresh_expires_at"] = now + seconds if seconds > 0 else None
        # Read-modify-write of the *whole* file, so it has to be serialized:
        # two processes refreshing different deployments concurrently would
        # each write back a snapshot taken before the other's, and one
        # deployment's entry would silently vanish into "Not logged in".
        with _cache_lock(self._cache_path):
            self._sweep_stale_temps()
            cache = self._load_cache()
            cache[self._cache_key()] = entry
            self._write_cache(cache)
        return entry

    def _sweep_stale_temps(self) -> None:
        """Remove temp files a hard-killed writer left behind. Call under the lock.

        The old fixed temp name self-healed — the next writer unlinked it. A
        unique name per writer cannot, so a process SIGKILLed between ``os.open``
        and ``os.replace`` would otherwise leave a 0600 fragment in the config
        directory forever. Anything older than a minute is abandoned: a real
        write of a few kilobytes does not take a minute, and a *live* writer is
        either holding this lock (so it is not running now) or is younger than
        the cutoff.
        """
        path = self._cache_path
        cutoff = time.time() - 60
        for stale in path.parent.glob(f"{path.name}.*.tmp"):
            try:
                if stale.stat().st_mtime < cutoff:
                    stale.unlink()
            except OSError:
                pass  # raced with another sweeper, or not ours to delete

    def _usable(self, entry: dict) -> bool:
        """Is this cached entry good for longer than the refresh skew?

        The skew is read AT CALL TIME so an operator can move it on a running
        process and a test's monkeypatch takes effect on the next call
        ([[les-witan-core-monkeypatch-constants]]).

        It is also capped at half this token's own lifetime — see
        ``_MAX_SKEW_FRACTION``. The lifetime comes from the entry itself rather
        than from configuration, because it is the IdP's decision and can differ
        per realm; ``obtained_at`` has always been stored, so this needs no
        cache-schema change and an entry written by an older client still works.
        """
        skew = _env_seconds(EXPIRY_SKEW_ENV_VAR, _EXPIRY_SKEW_S)
        lifetime = entry["expires_at"] - entry.get("obtained_at", entry["expires_at"])
        if 0 < lifetime < skew / _MAX_SKEW_FRACTION:
            # ★ AND SAY SO. Capping keeps the token usable and avoids a refresh
            # on every single call, but it does NOT make the token long enough:
            # a call slower than the capped skew can still expire in flight, and
            # a write that does cannot be safely retried. Nothing downstream can
            # infer that from a boolean, so the compromise is logged where an
            # operator can see it rather than left to be rediscovered from 401s.
            #
            # Not an outright failure, deliberately: refusing every request is a
            # worse answer than serving them with a shorter safety bound, and
            # this is reachable purely by an IdP's token-lifetime setting. If
            # the calls here are genuinely slower than half the token lifetime,
            # the fix is the IdP's TTL, not this constant.
            _log.warning(
                "access token lifetime (%.0fs) is short relative to the "
                "configured refresh skew (%.0fs); capping the skew at %.0fs. "
                "Calls slower than that can still have their token expire "
                "mid-flight — raise the IdP's token TTL.",
                lifetime,
                skew,
                lifetime * _MAX_SKEW_FRACTION,
            )
            skew = lifetime * _MAX_SKEW_FRACTION
        return entry["expires_at"] - time.time() > skew

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

    def _request_device_code(
        self, meta: dict, client: httpx2.Client
    ) -> httpx2.Response:
        """Start the device grant, asking for a session that outlives one call.

        ★ WHY ``offline_access`` AT ALL. Without it the realm's ordinary SSO
        session governs the refresh token, and on this deployment that is about
        five minutes — short enough that a login goes stale while the work it
        was for is still running. The concurrency probe made this concrete on
        2026-08-16: it needs ~8 minutes across two phases, pinned a token fine
        for the first, and got ``invalid_grant`` on the second because the
        session had already ended. No retry helps; a fresh login dies at the
        same point. ``offline_access`` is what Keycloak offers for exactly this
        — a refresh token whose life is not tied to the interactive session, and
        which it advertises as ``refresh_expires_in: 0`` (``_store_token``
        already understands that as "never", not "already expired").

        ★ AND THE TRADE, STATED PLAINLY: the cached refresh token stops
        expiring on its own, so a stolen cache file is usable until the grant is
        revoked at the IdP rather than until the session lapses. That is a real
        widening. It is accepted because the cache is written by ``os.open``
        with ``0o600`` from creation — never briefly group- or world-readable —
        and because the alternative on offer was raising the realm's SSO
        timeouts, which would lengthen EVERY session for every client rather
        than just this credential. Revocation moves to the IdP; there is no
        longer a short clock doing it for us.

        ★ FALLS BACK RATHER THAN FAILING. A realm that does not grant
        ``offline_access`` to this client answers the device-authorization
        endpoint with ``invalid_scope``, and raising there would turn a
        convenience into "nobody can log in at all" — strictly worse than the
        short session this exists to fix. So the scope is requested, and a
        refusal of THAT SPECIFIC KIND retries with plain ``openid``. Any other
        failure still propagates: it is not a scope problem and hiding it would
        cost a real diagnosis.
        """
        endpoint = meta["device_authorization_endpoint"]
        resp = client.post(
            endpoint, data={**self._auth_params(), "scope": _LOGIN_SCOPE}
        )
        if resp.status_code == 200:
            return resp
        # ★ THE STATUS IS PART OF THE TEST, not just the body. RFC 6749 §5.2
        # defines `invalid_scope` as a 400, so a 500 that happens to carry that
        # string is an outage wearing a scope error's clothes — retrying it
        # would contradict the guarantee stated above and bury the original
        # failure behind an identical second one.
        if (
            resp.status_code == _SCOPE_REJECTED_STATUS
            and _json_safe(resp).get("error") == "invalid_scope"
        ):
            resp = client.post(
                endpoint,
                data={**self._auth_params(), "scope": _FALLBACK_LOGIN_SCOPE},
            )
        resp.raise_for_status()
        return resp

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
            resp = self._request_device_code(meta, client)
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
        try:
            resp = client.post(
                meta["token_endpoint"],
                data={
                    **self._auth_params(),
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            )
        except httpx2.HTTPError as exc:
            # The one HTTP call in this module that used to let a transport
            # failure escape raw, while `discover_endpoints` and `login` both
            # wrapped theirs. A timeout or connection reset here is exactly the
            # "renewal failed but the session is fine" case, and it reached the
            # CLI as an httpx2 traceback instead of the sentence below.
            raise RemoteAuthError(
                f"Could not reach the token endpoint to renew the session: "
                f"{exc}. The refresh token was never sent, so the login is "
                "intact — this is retryable."
            ) from exc
        if resp.status_code != 200:
            # ★ "COULD NOT RENEW" IS NOT "YOU ARE LOGGED OUT". Every non-200
            # used to become `NeedsLogin`, so a 503, a proxy hiccup or a realm
            # restarting told the user their session had ended and sent them
            # through a device flow — discarding a refresh token that was
            # perfectly good. The IdP already distinguishes the two cases; only
            # `invalid_grant` means the grant itself is finished.
            error = str(_json_safe(resp).get("error", ""))
            if error in _DEAD_GRANT_ERRORS:
                raise NeedsLogin(
                    f"Session expired — run `{self._login_hint}` to re-authenticate."
                )
            detail = f" ({error})" if error else ""
            if resp.status_code in _RETRYABLE_STATUSES:
                raise RemoteAuthError(
                    f"Could not renew the session: the token endpoint answered "
                    f"HTTP {resp.status_code}{detail}. The refresh token was not "
                    "rejected, so the login is intact — this is retryable."
                )
            raise RemoteAuthError(
                f"Could not renew the session: the token endpoint rejected the "
                f"request with HTTP {resp.status_code}{detail}. The refresh "
                "token was NOT rejected, so re-authenticating will not help, "
                "and neither will retrying — this is a client or realm "
                "configuration fault. Check the configured client id against "
                "the realm's settings."
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
        if self._usable(entry):
            return entry["access_token"]
        if not entry.get("refresh_token"):
            raise NeedsLogin(
                f"Session expired — run `{self._login_hint}` to re-authenticate."
            )
        owns = client is None
        client = client or httpx2.Client(timeout=15)
        try:
            with _cache_lock(self._cache_path):
                # Re-read under the lock. A fleet of agents that started
                # together re-converges on the same expiry every ~5 minutes and
                # arrives here at once; whoever held the lock immediately
                # before us has almost certainly just stored a fresh token, so
                # the stampede collapses into one refresh and N-1 cache hits.
                # It also stops N processes from spending the *same* refresh
                # token, which a rotating IdP treats as replay.
                current = self._load_cache().get(self._cache_key()) or entry
                if self._usable(current):
                    return current["access_token"]
                refresh_token = current.get("refresh_token") or entry["refresh_token"]
                return self._refresh(refresh_token, client)["access_token"]
        finally:
            if owns:
                client.close()

    def force_refresh(
        self, rejected: str, *, client: httpx2.Client | None = None
    ) -> str:
        """Mint a new access token, ignoring how healthy the cached one looks.

        For the one case :meth:`get_valid_token` cannot serve: the DEPLOYMENT
        rejected a token this client still considers usable. Asking again
        through the normal path would return that same token from cache and
        turn a retry into a second 401, so the cache check has to be skipped
        rather than re-run.

        ★ ``rejected`` IS THE TOKEN THAT ACTUALLY FAILED, PASSED IN — not read
        back out of the cache here. Reading it here looked equivalent and is
        not: under concurrent 401s, another worker can replace the cache
        between the failure and this call, so this would treat ITS fresh token
        as the rejected one and refresh a perfectly good credential. Repeated
        across workers that serialises into one refresh each, defeating the
        single-flight behaviour below and spending rotating refresh tokens a
        rotating IdP treats as replay.

        Still takes the cache lock and still re-reads under it, for the reason
        :meth:`get_valid_token` documents — a fleet that hits an expiry
        boundary together arrives here together, and whoever refreshed a moment
        ago has already stored a good token. A cached token is accepted when it
        differs from the one that was rejected; an identical one is refreshed.
        """
        entry = self._load_cache().get(self._cache_key())
        if not entry:
            raise NeedsLogin(
                f"Not logged in to {self._cfg.url} — run `{self._login_hint}` first."
            )
        if not entry.get("refresh_token"):
            raise NeedsLogin(
                f"Session expired — run `{self._login_hint}` to re-authenticate."
            )
        owns = client is None
        client = client or httpx2.Client(timeout=15)
        try:
            with _cache_lock(self._cache_path):
                current = self._load_cache().get(self._cache_key()) or entry
                # Somebody else already replaced it — take theirs rather than
                # spending a second refresh token on the same expiry, which a
                # rotating IdP treats as replay.
                if current.get("access_token") != rejected and self._usable(current):
                    return current["access_token"]
                refresh_token = current.get("refresh_token") or entry["refresh_token"]
                return self._refresh(refresh_token, client)["access_token"]
        finally:
            if owns:
                client.close()

    def cached_claims(self) -> dict:
        """Claims of the cached access token for this deployment, or ``{}``.

        Deliberately offline and expiry-blind, unlike
        :meth:`get_valid_token`: the only thing read through here is *who the
        user is* (``sub``), which does not change when a token expires and is
        still the right answer while a refresh is pending. Callers that need
        to actually *call* the deployment use ``get_valid_token``; callers
        that only need an identity — witan-code naming the branch views it
        owns — must not pay a network round trip, or block, to learn it.
        """
        entry = self._load_cache().get(self._cache_key())
        if not entry:
            return {}
        return decode_claims(entry.get("access_token", ""))

    def session_life(self) -> SessionLife:
        """When the cached access token and the login behind it run out.

        Offline and refresh-free, like :meth:`cached_claims` and for the same
        reason: this is asked in order to *report* on a session, and a call that
        renewed the thing it was measuring would change the answer by asking.

        Everything absent when there is no cached entry at all —
        ``refresh_state`` is then ``"unknown"``, which is the truth: nothing is
        known about a login that was never made.
        """
        entry = self._load_cache().get(self._cache_key()) or {}
        if "refresh_expires_at" not in entry:
            state, refresh_at = "unknown", None
        elif entry["refresh_expires_at"] is None:
            state, refresh_at = "never", None
        else:
            state, refresh_at = "finite", float(entry["refresh_expires_at"])
        return SessionLife(
            entry.get("expires_at"), refresh_at, state, bool(entry.get("refresh_token"))
        )

    def logout(self) -> bool:
        """Drop the cached token for this deployment. True if one existed."""
        with _cache_lock(self._cache_path):
            cache = self._load_cache()
            existed = cache.pop(self._cache_key(), None) is not None
            if existed:
                self._write_cache(cache)
        return existed

    def token_provider(self) -> Callable[[], str]:
        """A zero-arg callable the proxy calls per request to get a fresh token."""
        return lambda: self.get_valid_token()


def cache_path() -> Path:
    """The token-cache location: ``$WITAN_TOKEN_CACHE`` or :data:`DEFAULT_CACHE_PATH`.

    ``~`` is expanded, matching every other path setting in these packages
    (``code_dir``, ``--store``). A shell expands ``~`` before the variable is
    ever set, but a value from a config file, Docker ``ENV``, or a systemd unit
    does not — and without this that override would silently create a directory
    literally named ``~`` under the cwd.
    """
    override = os.environ.get("WITAN_TOKEN_CACHE")
    return Path(override).expanduser() if override else DEFAULT_CACHE_PATH


def device_auth(endpoint: OidcEndpoint, *, login_hint: str) -> DeviceAuth:
    """A :class:`DeviceAuth` on the shared token cache, hinting ``login_hint``."""
    return DeviceAuth(endpoint, cache_path(), login_hint=login_hint)
