"""Device-code login, token cache, and refresh (witan_core.remote.oidc).

The transport-agnostic core of ADR-0005 path a. Each server binds its own
cache path + login hint (see witan-council's shim); here we drive DeviceAuth
directly against a tmp cache and an httpx2.MockTransport.
"""

from __future__ import annotations

import base64
import json
import multiprocessing
import os
import stat
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest

from witan_core.remote import oidc
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


def _client(handler) -> httpx2.Client:
    return httpx2.Client(transport=httpx2.MockTransport(handler))


def test_decode_claims_is_display_only():
    tok = _jwt({"sub": "abc", "preferred_username": "alice"})
    assert decode_claims(tok)["preferred_username"] == "alice"
    assert decode_claims("not-a-jwt") == {}


def test_login_polls_until_approved_then_caches(auth, cache_path):
    access = _jwt({"sub": "u-1", "preferred_username": "alice"})
    calls = {"n": 0}

    def handler(req: httpx2.Request) -> httpx2.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx2.Response(200, json=_META)
        if str(req.url) == _META["device_authorization_endpoint"]:
            return httpx2.Response(
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
            return httpx2.Response(400, json={"error": "authorization_pending"})
        return httpx2.Response(
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

    def handler(req: httpx2.Request) -> httpx2.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx2.Response(200, json=_META)
        if str(req.url) == _META["device_authorization_endpoint"]:
            return httpx2.Response(
                200, json={"device_code": "d", "user_code": "C", "interval": 5}
            )
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx2.Response(400, json={"error": "slow_down"})
        return httpx2.Response(200, json={"access_token": access, "expires_in": 60})

    auth.login(on_prompt=lambda _d: None, client=_client(handler), sleep=seen.append)
    # First sleep at base interval 5, second after slow_down bumps it to 10.
    assert seen == [5, 10]


def test_login_access_denied_raises(auth):
    def handler(req: httpx2.Request) -> httpx2.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx2.Response(200, json=_META)
        if str(req.url) == _META["device_authorization_endpoint"]:
            return httpx2.Response(200, json={"device_code": "d", "user_code": "C"})
        return httpx2.Response(400, json={"error": "access_denied"})

    with pytest.raises(RemoteAuthError, match="access_denied"):
        auth.login(
            on_prompt=lambda _d: None, client=_client(handler), sleep=lambda _s: None
        )


def test_login_retries_with_a_fresh_code_after_expired_token(auth):
    """A relay-approved login that lapses once still succeeds, transparently."""
    access = _jwt({"sub": "u"})
    device_codes_issued = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx2.Response(200, json=_META)
        if str(req.url) == _META["device_authorization_endpoint"]:
            code = f"dev-{len(device_codes_issued)}"
            device_codes_issued.append(code)
            return httpx2.Response(
                200, json={"device_code": code, "user_code": code.upper()}
            )
        # token endpoint: the first device code always looks expired; the
        # second one succeeds immediately.
        body = dict(urllib.parse.parse_qsl(req.content.decode()))
        if body.get("device_code") == device_codes_issued[0]:
            return httpx2.Response(400, json={"error": "expired_token"})
        return httpx2.Response(200, json={"access_token": access, "expires_in": 60})

    prompts = []
    claims = auth.login(
        on_prompt=prompts.append, client=_client(handler), sleep=lambda _s: None
    )
    assert claims["sub"] == "u"
    # Prompted twice — once per device code — and it took a second code to land.
    assert [p["user_code"] for p in prompts] == ["DEV-0", "DEV-1"]


def test_login_gives_up_after_repeated_expired_tokens(auth):
    def handler(req: httpx2.Request) -> httpx2.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx2.Response(200, json=_META)
        if str(req.url) == _META["device_authorization_endpoint"]:
            return httpx2.Response(200, json={"device_code": "d", "user_code": "C"})
        return httpx2.Response(400, json={"error": "expired_token"})

    prompts = []
    with pytest.raises(RemoteAuthError, match="after 3 attempt"):
        auth.login(
            on_prompt=prompts.append, client=_client(handler), sleep=lambda _s: None
        )
    assert len(prompts) == 3


def test_login_requests_a_fresh_code_when_the_local_deadline_elapses(auth):
    """The other way a code lapses: the local `expires_in` deadline passes
    with no definitive answer yet — not every relay failure comes back as an
    explicit `expired_token` from the IdP before the client stops polling.
    """
    access = _jwt({"sub": "u"})
    device_codes_issued = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx2.Response(200, json=_META)
        if str(req.url) == _META["device_authorization_endpoint"]:
            code = f"dev-{len(device_codes_issued)}"
            device_codes_issued.append(code)
            # The first code is already-lapsed locally; the second is fine.
            expires_in = 0 if code == "dev-0" else 300
            return httpx2.Response(
                200,
                json={
                    "device_code": code,
                    "user_code": code.upper(),
                    "expires_in": expires_in,
                },
            )
        # Must never be polled for dev-0 — its deadline was already past
        # before the poll loop's first `time.time()` check.
        body = dict(urllib.parse.parse_qsl(req.content.decode()))
        assert body.get("device_code") != device_codes_issued[0]
        return httpx2.Response(200, json={"access_token": access, "expires_in": 60})

    prompts = []
    claims = auth.login(
        on_prompt=prompts.append, client=_client(handler), sleep=lambda _s: None
    )
    assert claims["sub"] == "u"
    assert [p["user_code"] for p in prompts] == ["DEV-0", "DEV-1"]


def test_non_json_metadata_raises_clean_error():
    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="<html>proxy error</html>")

    from witan_core.remote.oidc import discover_endpoints

    with pytest.raises(RemoteAuthError, match="non-JSON"):
        discover_endpoints(_Endpoint().oidc_issuer, client=_client(handler))


def test_issuer_mismatch_is_refused():
    """An AS mix-up presents exactly this way: reachable metadata at the
    configured issuer's well-known path that points elsewhere."""
    from witan_core.remote.oidc import discover_endpoints

    evil = {**_META, "issuer": "https://attacker.example.net/realms/ol"}

    def handler(_req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=evil)

    with pytest.raises(RemoteAuthError, match="does not match the configured issuer"):
        discover_endpoints(_Endpoint().oidc_issuer, client=_client(handler))


def test_missing_issuer_is_refused():
    from witan_core.remote.oidc import discover_endpoints

    bare = {k: v for k, v in _META.items() if k != "issuer"}

    def handler(_req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=bare)

    with pytest.raises(RemoteAuthError, match="advertises issuer None"):
        discover_endpoints(_Endpoint().oidc_issuer, client=_client(handler))


@pytest.mark.parametrize("body", [[], "nope", 3])
def test_non_object_metadata_raises_clean_error(body):
    """A 200 that parses as JSON but isn't an object must not AttributeError on
    the issuer lookup — same failure class as the non-JSON case."""
    from witan_core.remote.oidc import discover_endpoints

    def handler(_req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=body)

    with pytest.raises(RemoteAuthError, match="not a JSON object"):
        discover_endpoints(_Endpoint().oidc_issuer, client=_client(handler))


def test_trailing_slash_is_not_a_mismatch():
    """The URL construction rstrips '/', so issuer comparison must too."""
    from witan_core.remote.oidc import discover_endpoints

    slashed = {**_META, "issuer": f"{_Endpoint.oidc_issuer}/"}

    def handler(_req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=slashed)

    meta = discover_endpoints(f"{_Endpoint().oidc_issuer}/", client=_client(handler))
    assert meta["token_endpoint"] == _META["token_endpoint"]


def test_non_json_token_response_raises_not_crashes(auth):
    # A 200 with a non-JSON body on the token endpoint must surface as a clean
    # RemoteAuthError, not an unhandled JSONDecodeError.
    def handler(req: httpx2.Request) -> httpx2.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx2.Response(200, json=_META)
        if str(req.url) == _META["device_authorization_endpoint"]:
            return httpx2.Response(200, json={"device_code": "d", "user_code": "C"})
        return httpx2.Response(200, text="not json")

    with pytest.raises(RemoteAuthError, match="non-JSON"):
        auth.login(
            on_prompt=lambda _d: None, client=_client(handler), sleep=lambda _s: None
        )


def test_non_json_error_body_falls_through_to_generic_error(auth):
    # A non-JSON *error* body must not crash the poll loop; err defaults to ""
    # and we raise the generic failure carrying the raw text.
    def handler(req: httpx2.Request) -> httpx2.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx2.Response(200, json=_META)
        if str(req.url) == _META["device_authorization_endpoint"]:
            return httpx2.Response(200, json={"device_code": "d", "user_code": "C"})
        return httpx2.Response(400, text="<html>gateway timeout</html>")

    with pytest.raises(RemoteAuthError, match="Device authorization failed"):
        auth.login(
            on_prompt=lambda _d: None, client=_client(handler), sleep=lambda _s: None
        )


def test_audience_is_sent_when_configured(cache_path):
    auth = DeviceAuth(_Endpoint(oidc_audience="witan-api"), cache_path)
    access = _jwt({"sub": "u"})
    seen: list[dict] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx2.Response(200, json=_META)
        # Capture the posted form for both device-auth and token calls.
        from urllib.parse import parse_qs

        seen.append({k: v[0] for k, v in parse_qs(req.content.decode()).items()})
        if str(req.url) == _META["device_authorization_endpoint"]:
            return httpx2.Response(200, json={"device_code": "d", "user_code": "C"})
        return httpx2.Response(200, json={"access_token": access, "expires_in": 60})

    auth.login(
        on_prompt=lambda _d: None, client=_client(handler), sleep=lambda _s: None
    )
    assert all(form.get("audience") == "witan-api" for form in seen)


def test_audience_absent_when_not_configured(auth):
    access = _jwt({"sub": "u"})
    seen: list[dict] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx2.Response(200, json=_META)
        from urllib.parse import parse_qs

        seen.append({k: v[0] for k, v in parse_qs(req.content.decode()).items()})
        if str(req.url) == _META["device_authorization_endpoint"]:
            return httpx2.Response(200, json={"device_code": "d", "user_code": "C"})
        return httpx2.Response(200, json={"access_token": access, "expires_in": 60})

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

    def handler(req: httpx2.Request) -> httpx2.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx2.Response(200, json=_META)
        # token endpoint — expect a refresh_token grant
        body = req.content.decode()
        assert "grant_type=refresh_token" in body
        return httpx2.Response(
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

    def handler(req: httpx2.Request) -> httpx2.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx2.Response(200, json=_META)
        return httpx2.Response(400, json={"error": "invalid_grant"})

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


# ── concurrent writers ─────────────────────────────────────────────────────
# A fleet of agents on one machine shares ~/.config/witan/tokens.json, and the
# access token's ~5-minute life makes them re-converge on a simultaneous
# refresh forever. These drive REAL processes released by a REAL barrier
# because that is what reproduced the live failure: threads inside one
# interpreter never hit the cross-process file races at all.

_FLEET = 16


def _store_worker(cache_path, index, barrier, result_path):
    """One agent process storing a token for its own deployment."""
    auth = DeviceAuth(_Endpoint(oidc_client_id=f"client-{index}"), cache_path)
    barrier.wait()
    try:
        auth._store_token({"access_token": f"tok-{index}", "expires_in": 900})
    except BaseException as exc:  # noqa: BLE001 — the outcome IS the assertion
        Path(result_path).write_text(f"{type(exc).__name__}: {exc}")
    else:
        Path(result_path).write_text("ok")


def _refresh_worker(cache_path, refresh_log, index, barrier, result_path):
    """One agent process finding the shared token expired and refreshing it."""
    auth = DeviceAuth(_Endpoint(), cache_path, login_hint="witan login")

    def handler(req: httpx2.Request) -> httpx2.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx2.Response(200, json=_META)
        with open(refresh_log, "a", encoding="utf-8") as fh:
            fh.write("refresh\n")  # short + O_APPEND: atomic across processes
        return httpx2.Response(
            200,
            json={
                "access_token": _jwt({"sub": "u"}),
                "refresh_token": "r-new",
                "expires_in": 900,
            },
        )

    barrier.wait()
    try:
        token = auth.get_valid_token(client=_client(handler))
    except BaseException as exc:  # noqa: BLE001
        Path(result_path).write_text(f"{type(exc).__name__}: {exc}")
    else:
        Path(result_path).write_text(f"ok {token}")


def _run_fleet(target, tmp_path, *args):
    """Start _FLEET processes, release them at one instant, collect outcomes."""
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(_FLEET)
    procs = []
    for i in range(_FLEET):
        result = tmp_path / f"result-{i}"
        procs.append(
            (result, ctx.Process(target=target, args=(*args, i, barrier, str(result))))
        )
    for _, proc in procs:
        proc.start()
    for _, proc in procs:
        proc.join(timeout=60)
    return [
        result.read_text() if result.exists() else "no result (process died)"
        for result, _ in procs
    ]


def test_concurrent_stores_all_succeed_and_no_entry_is_lost(cache_path, tmp_path):
    # Each process writes a DIFFERENT deployment's entry, so a lost write is
    # unambiguous: a read-modify-write of one shared file must not drop the
    # entry a concurrent writer just added.
    results = _run_fleet(_store_worker, tmp_path, cache_path)

    assert results == ["ok"] * _FLEET
    cache = json.loads(cache_path.read_text())
    assert len(cache) == _FLEET
    for i in range(_FLEET):
        assert (
            cache[f"{_Endpoint.oidc_issuer}|client-{i}"]["access_token"] == f"tok-{i}"
        )
    # No temp file survives, and the cache is still owner-only.
    assert list(cache_path.parent.glob("tokens.json*.tmp")) == []
    assert stat.S_IMODE(cache_path.stat().st_mode) == 0o600


def test_store_sweeps_a_hard_killed_writers_temp(auth, cache_path):
    # The old fixed temp name self-healed via the pre-unlink; a unique name
    # cannot, so a SIGKILLed writer's fragment must be swept instead.
    abandoned = cache_path.with_name(f"{cache_path.name}.999999.deadbeef.tmp")
    abandoned.write_text("{}")
    os.utime(abandoned, (time.time() - 3600, time.time() - 3600))
    mine = cache_path.with_name(f"{cache_path.name}.999998.cafe.tmp")
    mine.write_text("{}")  # in-flight: too young to be abandoned

    auth._store_token({"access_token": "t", "expires_in": 900})

    assert not abandoned.exists()
    assert mine.exists()


def test_concurrent_refreshes_collapse_into_one(cache_path, tmp_path):
    # All processes start from the same expired entry, so without single-flight
    # every one of them spends the same refresh token — which a rotating IdP
    # reads as replay, and which is what put 401s in front of the fleet.
    DeviceAuth(_Endpoint(), cache_path)._write_cache(
        {
            f"{_Endpoint.oidc_issuer}|{_Endpoint.oidc_client_id}": {
                "access_token": _jwt({"sub": "u"}),
                "refresh_token": "r-old",
                "expires_at": time.time() - 100,
            }
        }
    )
    refresh_log = tmp_path / "refreshes"
    refresh_log.touch()

    results = _run_fleet(_refresh_worker, tmp_path, cache_path, str(refresh_log))

    assert all(r.startswith("ok ") for r in results), results
    assert len(set(results)) == 1  # everyone ends up on the same token
    assert refresh_log.read_text().count("refresh") == 1


# ── the refresh skew has to outlast the call it is fetched for ─────────────
# OBSERVED LIVE 2026-08-13: 8 of 24 concurrent writers got
# `"POST /mcp HTTP/1.1" 401 Unauthorized` mid-run. `default_token_provider` is
# consulted PER REQUEST and returned any token with >30s of life, while a single
# `memory_store` was measured at 3-51s against the same deployment. So a token
# with 31 seconds left was handed to a call needing 45 and died in flight —
# tk-the-oidc-refresh-skew-30s-is-shorter-than-a-writ-8b04db.


def _entry(*, lifetime: float, remaining: float) -> dict:
    now = time.time()
    return {
        "access_token": "a",
        "refresh_token": "r",
        "expires_at": now + remaining,
        "obtained_at": now + remaining - lifetime,
    }


def test_a_token_that_cannot_outlast_a_slow_write_is_not_usable(auth):
    """★ THE REGRESSION. 40s of life used to pass (>30) and be handed to a call
    that can take 51s. It must not."""
    assert not auth._usable(_entry(lifetime=300, remaining=40))


def test_a_token_with_room_for_the_slowest_write_is_usable(auth):
    assert auth._usable(_entry(lifetime=300, remaining=120))


def test_the_skew_is_read_at_call_time(auth, monkeypatch):
    """So an operator can move it on a running process — the same property the
    remote-write knobs have, and for the same reason: the number came from one
    deployment's measured write cost."""
    entry = _entry(lifetime=300, remaining=40)
    assert not auth._usable(entry)
    monkeypatch.setenv(oidc.EXPIRY_SKEW_ENV_VAR, "10")
    assert auth._usable(entry)


@pytest.mark.parametrize("value", ["not-a-number", "-5", "nan", "inf"])
def test_an_unusable_skew_override_falls_back_rather_than_raising(
    auth, monkeypatch, value
):
    """A typo in a deployment env var must not turn every authenticated request
    into a crash. `nan` matters most: float() accepts it and it compares False
    against every bound, so a negative-only guard would let it through and make
    `_usable` silently always-false — a refresh on every single call."""
    monkeypatch.setenv(oidc.EXPIRY_SKEW_ENV_VAR, value)
    assert auth._usable(_entry(lifetime=300, remaining=120))
    assert not auth._usable(_entry(lifetime=300, remaining=40))


def test_the_skew_is_capped_at_half_a_short_token_s_life(auth, monkeypatch):
    """★ A SKEW LONGER THAN THE TOKEN WOULD REFRESH ON EVERY CALL.

    An IdP issuing 60s tokens against a 90s skew makes no entry ever usable, so
    every request mints a new token and a fleet stampedes the token endpoint —
    the exact failure the cache lock exists to prevent. Capped at half the
    token's own life, a short token is still used for half of it.
    """
    monkeypatch.setenv(oidc.EXPIRY_SKEW_ENV_VAR, "90")
    # 60s token, 40s left: uncapped this fails (40 < 90); capped at 30 it passes.
    assert auth._usable(_entry(lifetime=60, remaining=40))
    assert not auth._usable(_entry(lifetime=60, remaining=20))


def test_capping_the_skew_is_logged_rather_than_silent(auth, monkeypatch, caplog):
    """★ THE CAP IS A COMPROMISE AND MUST NOT BE INVISIBLE.

    It keeps the token usable and avoids refreshing on every call, but it does
    not make the token long enough: a call slower than the capped skew can still
    expire in flight, and a write that does cannot be safely retried. A boolean
    return cannot carry that, so it is logged where an operator can see it
    instead of being rediscovered from 401s.
    """
    monkeypatch.setenv(oidc.EXPIRY_SKEW_ENV_VAR, "90")
    with caplog.at_level("WARNING"):
        assert auth._usable(_entry(lifetime=60, remaining=40))
    assert "capping the skew" in caplog.text
    assert "mid-flight" in caplog.text


def test_a_long_token_caps_nothing_and_logs_nothing(auth, monkeypatch, caplog):
    """The normal case — a ~5min token against a 90s skew — must stay quiet, or
    the warning becomes noise nobody reads."""
    monkeypatch.setenv(oidc.EXPIRY_SKEW_ENV_VAR, "90")
    with caplog.at_level("WARNING"):
        assert auth._usable(_entry(lifetime=300, remaining=200))
    assert "capping the skew" not in caplog.text


# ── offline_access at login ───────────────────────────────────────────────
# The refresh token's life decides whether a login survives the work it was for.
# Without `offline_access` that is the interactive SSO session's life, which on
# this deployment is ~5 minutes — short enough that the concurrency probe could
# not finish a two-phase run (it pinned a token for phase A, then got
# `invalid_grant` for phase B). These pin the request and, more importantly, the
# fallback: a realm that refuses the scope must still be able to log in.


def _device_flow_handler(seen_scopes: list[str], *, device_status):
    """Handler recording every scope asked of the device endpoint.

    ``device_status`` maps the requested scope to the status to answer with, so
    a test can have `offline_access` refused while plain `openid` succeeds.
    """
    access = _jwt({"sub": "u-1", "preferred_username": "alice"})

    def handler(req: httpx2.Request) -> httpx2.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx2.Response(200, json=_META)
        if str(req.url) == _META["device_authorization_endpoint"]:
            scope = httpx2.QueryParams(req.content.decode()).get("scope", "")
            seen_scopes.append(scope)
            if device_status(scope) != 200:
                return httpx2.Response(400, json={"error": "invalid_scope"})
            return httpx2.Response(
                200,
                json={
                    "device_code": "dev-code",
                    "user_code": "WXYZ",
                    "verification_uri": "https://sso.example.org/device",
                    "interval": 1,
                    "expires_in": 300,
                },
            )
        return httpx2.Response(
            200,
            json={"access_token": access, "refresh_token": "r-1", "expires_in": 300},
        )

    return handler


def test_login_asks_for_offline_access(auth):
    """The whole point: a refresh token not tied to the SSO session."""
    seen: list[str] = []
    auth.login(
        on_prompt=lambda _d: None,
        client=_client(_device_flow_handler(seen, device_status=lambda _s: 200)),
        sleep=lambda _s: None,
    )
    assert seen == ["openid offline_access"]


def test_login_falls_back_when_the_realm_refuses_offline_access(auth):
    """★ A realm that does not grant the scope must still be able to log in.

    Raising on `invalid_scope` would turn a convenience into a total outage of
    the login path — strictly worse than the short session being fixed.
    """
    seen: list[str] = []
    claims = auth.login(
        on_prompt=lambda _d: None,
        client=_client(
            _device_flow_handler(
                seen,
                device_status=lambda s: 400 if "offline_access" in s else 200,
            )
        ),
        sleep=lambda _s: None,
    )
    assert seen == ["openid offline_access", "openid"]
    assert claims["preferred_username"] == "alice"


def test_a_non_scope_failure_at_the_device_endpoint_is_not_retried(auth):
    """Only `invalid_scope` is retried. A 500 is not a scope problem, and
    retrying it would hide a real diagnosis behind a second identical failure.
    """
    seen: list[str] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx2.Response(200, json=_META)
        if str(req.url) == _META["device_authorization_endpoint"]:
            seen.append("hit")
            return httpx2.Response(500, json={"error": "server_error"})
        raise AssertionError("token endpoint must not be reached")

    with pytest.raises(RemoteAuthError):
        auth.login(
            on_prompt=lambda _d: None,
            client=_client(handler),
            sleep=lambda _s: None,
        )
    assert seen == ["hit"], "a non-scope failure must not be retried"


def test_an_invalid_scope_body_on_a_500_is_not_retried(auth):
    """★ The status is part of the test, not just the body.

    RFC 6749 §5.2 defines `invalid_scope` as a 400. A 500 carrying that string
    is an outage wearing a scope error's clothes — retrying it would contradict
    the no-retry guarantee and bury the original failure behind an identical
    second one. Matching on the body alone (the first version of this) passed
    every other test here, because none of them sent that combination.
    """
    seen: list[str] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx2.Response(200, json=_META)
        if str(req.url) == _META["device_authorization_endpoint"]:
            seen.append("hit")
            return httpx2.Response(500, json={"error": "invalid_scope"})
        raise AssertionError("token endpoint must not be reached")

    with pytest.raises(RemoteAuthError):
        auth.login(
            on_prompt=lambda _d: None,
            client=_client(handler),
            sleep=lambda _s: None,
        )
    assert seen == ["hit"], "invalid_scope on a non-400 must not be retried"
