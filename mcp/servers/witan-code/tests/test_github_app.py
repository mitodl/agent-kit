"""Tests for witan_code.github_app — App JWT minting and the token exchange.

The token exchange runs against an ``httpx2.MockTransport`` (same approach as
``packages/witan-core/tests/test_remote_oidc.py``); the JWT is signed with a
throwaway RSA key generated per session and verified by decoding it back, so
these cover the real crypto path without needing a real App.
"""

import time

import httpx2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from joserfc import jwt
from joserfc.jwk import RSAKey
from witan_code.github_app import (
    API_URL_ENV_VAR,
    APP_ID_ENV_VAR,
    EXIT_ERROR,
    EXIT_NOT_CONFIGURED,
    EXIT_OK,
    GITHUB_API_URL,
    INSTALLATION_ID_ENV_VAR,
    KEY_FILE_ENV_VAR,
    AppCredentials,
    GitHubAppError,
    app_jwt,
    from_env,
    installation_token,
    main,
)

_JWT_MAX_LIFETIME = 600  # GitHub's hard limit on an App JWT.


@pytest.fixture(scope="session")
def pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def credentials(pem: str) -> AppCredentials:
    return AppCredentials(app_id="123456", installation_id="789", private_key=pem)


def _client(handler) -> httpx2.Client:
    return httpx2.Client(transport=httpx2.MockTransport(handler))


# ── app_jwt ───────────────────────────────────────────────────────────────────


def test_app_jwt_claims_are_what_github_requires(credentials, pem):
    now = 1_700_000_000
    decoded = jwt.decode(app_jwt(credentials, now=now), RSAKey.import_key(pem))

    assert decoded.header["alg"] == "RS256"
    assert decoded.claims["iss"] == "123456"
    # Backdated against clock skew: GitHub rejects a JWT issued in the future.
    assert decoded.claims["iat"] < now
    # And inside GitHub's 10-minute ceiling, measured from the backdated iat
    # rather than from now — that is the span GitHub actually validates.
    assert decoded.claims["exp"] - decoded.claims["iat"] <= _JWT_MAX_LIFETIME


def test_app_jwt_defaults_to_the_current_time(credentials, pem):
    before = int(time.time())
    decoded = jwt.decode(app_jwt(credentials), RSAKey.import_key(pem))
    assert decoded.claims["exp"] > before


def test_app_jwt_rejects_a_key_that_is_not_a_pem(credentials):
    broken = AppCredentials(
        app_id="1", installation_id="2", private_key="-----BEGIN NOPE-----"
    )
    with pytest.raises(GitHubAppError, match="not a usable RSA PEM"):
        app_jwt(broken)


# ── installation_token ────────────────────────────────────────────────────────


def test_installation_token_posts_to_the_installation_and_returns_the_token(
    credentials, pem
):
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["accept"] = request.headers["Accept"]
        seen["method"] = request.method
        return httpx2.Response(201, json={"token": "ghs_secret", "expires_at": "x"})

    token = installation_token(credentials, client=_client(handler))

    assert token == "ghs_secret"
    assert seen["method"] == "POST"
    assert seen["url"] == ("https://api.github.com/app/installations/789/access_tokens")
    assert seen["accept"] == "application/vnd.github+json"
    # The bearer is the App JWT, and it must verify against the App's own key.
    presented = seen["auth"].removeprefix("Bearer ")
    assert jwt.decode(presented, RSAKey.import_key(pem)).claims["iss"] == "123456"


def test_installation_token_raises_on_a_refusal(credentials):
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, json={"message": "Not Found"})

    with pytest.raises(GitHubAppError, match="HTTP 404"):
        installation_token(credentials, client=_client(handler))


def test_installation_token_raises_when_the_body_has_no_token(credentials):
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(201, json={"expires_at": "x"})

    with pytest.raises(GitHubAppError, match="no token"):
        installation_token(credentials, client=_client(handler))


def test_installation_token_raises_on_an_empty_token(credentials):
    """An empty string would otherwise be exported as a credential and 401."""

    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(201, json={"token": ""})

    with pytest.raises(GitHubAppError, match="empty token"):
        installation_token(credentials, client=_client(handler))


def test_installation_token_raises_when_github_is_unreachable(credentials):
    def handler(_: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route")

    with pytest.raises(GitHubAppError, match="Could not reach"):
        installation_token(credentials, client=_client(handler))


def test_installation_token_leaves_a_caller_supplied_client_open(credentials):
    """A client the caller owns is theirs to close, even on the error path."""

    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(201, json={"token": "ghs_secret"})

    client = _client(handler)
    installation_token(credentials, client=client)
    assert not client.is_closed


def test_installation_token_closes_a_client_it_created(credentials, monkeypatch):
    """Otherwise a long-lived caller accumulates connections per call."""
    created: list[httpx2.Client] = []
    real_client = httpx2.Client

    def spy(*args, **kwargs):
        client = real_client(
            *args,
            **kwargs,
            transport=httpx2.MockTransport(
                lambda _: httpx2.Response(201, json={"token": "ghs_secret"})
            ),
        )
        created.append(client)
        return client

    monkeypatch.setattr(httpx2, "Client", spy)
    installation_token(credentials)

    assert len(created) == 1
    assert created[0].is_closed


# ── from_env ──────────────────────────────────────────────────────────────────


def _env(tmp_path, pem, **overrides) -> dict[str, str]:
    key_file = tmp_path / "app.pem"
    key_file.write_text(pem)
    env = {
        APP_ID_ENV_VAR: "123456",
        INSTALLATION_ID_ENV_VAR: "789",
        KEY_FILE_ENV_VAR: str(key_file),
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


def test_from_env_none_when_nothing_is_configured():
    assert from_env({}) is None


def test_from_env_reads_the_key_off_disk(tmp_path, pem):
    creds = from_env(_env(tmp_path, pem))
    assert creds is not None
    assert creds.app_id == "123456"
    assert creds.installation_id == "789"
    assert creds.private_key == pem


@pytest.mark.parametrize(
    "missing", [APP_ID_ENV_VAR, INSTALLATION_ID_ENV_VAR, KEY_FILE_ENV_VAR]
)
def test_from_env_rejects_a_partial_configuration(tmp_path, pem, missing):
    """Half-configured must not read as unconfigured — see the module docstring."""
    with pytest.raises(GitHubAppError, match="Incomplete GitHub App configuration"):
        from_env(_env(tmp_path, pem, **{missing: None}))


def test_from_env_rejects_an_empty_key_file(tmp_path, pem):
    """What an unfulfilled Vault-synced Secret looks like."""
    env = _env(tmp_path, pem)
    (tmp_path / "app.pem").write_text("")
    with pytest.raises(GitHubAppError, match="is empty"):
        from_env(env)


def test_from_env_rejects_a_missing_key_file(tmp_path, pem):
    with pytest.raises(GitHubAppError, match="Cannot read"):
        from_env(_env(tmp_path, pem, **{KEY_FILE_ENV_VAR: str(tmp_path / "nope.pem")}))


def test_from_env_rejects_a_non_utf8_key_file(tmp_path, pem):
    """A truncated or binary mount, which a PEM never is — same clear error."""
    env = _env(tmp_path, pem)
    (tmp_path / "app.pem").write_bytes(b"\xff\xfe not a pem \x00")
    with pytest.raises(GitHubAppError, match="Cannot read"):
        from_env(env)


def test_credentials_repr_does_not_leak_the_key(credentials):
    """These land in tracebacks; the PEM must not ride along."""
    assert "BEGIN" not in repr(credentials)
    assert "<redacted>" in repr(credentials)


# ── main / exit codes ─────────────────────────────────────────────────────────


def test_main_check_reports_not_configured(monkeypatch):
    for name in (APP_ID_ENV_VAR, INSTALLATION_ID_ENV_VAR, KEY_FILE_ENV_VAR):
        monkeypatch.delenv(name, raising=False)
    assert main(["--check"]) == EXIT_NOT_CONFIGURED


def test_main_check_succeeds_without_minting(tmp_path, pem, monkeypatch):
    for name, value in _env(tmp_path, pem).items():
        monkeypatch.setenv(name, value)
    # No transport is patched, so a token exchange here would fail outright —
    # which is the assertion: --check must not make one.
    assert main(["--check"]) == EXIT_OK


def test_main_check_errors_on_a_partial_configuration(tmp_path, pem, monkeypatch):
    monkeypatch.delenv(INSTALLATION_ID_ENV_VAR, raising=False)
    monkeypatch.setenv(APP_ID_ENV_VAR, "123456")
    monkeypatch.setenv(KEY_FILE_ENV_VAR, str(tmp_path / "app.pem"))
    (tmp_path / "app.pem").write_text(pem)
    assert main(["--check"]) == EXIT_ERROR


def test_main_prints_the_token(tmp_path, pem, monkeypatch, capsys):
    for name, value in _env(tmp_path, pem).items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(API_URL_ENV_VAR, raising=False)
    seen = {}

    def fake(credentials, *, api_url):
        seen["api_url"] = api_url
        return "ghs_printed"

    monkeypatch.setattr("witan_code.github_app.installation_token", fake)
    assert main([]) == EXIT_OK
    assert capsys.readouterr().out.strip() == "ghs_printed"
    assert seen["api_url"] == GITHUB_API_URL


def test_main_honours_an_api_url_override(tmp_path, pem, monkeypatch, capsys):
    """What lets the entrypoint's App path be exercised against a stub API."""
    for name, value in _env(tmp_path, pem).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(API_URL_ENV_VAR, "http://127.0.0.1:9999")
    monkeypatch.setattr(
        "witan_code.github_app.installation_token",
        lambda _creds, *, api_url: f"ghs_{api_url}",
    )
    assert main([]) == EXIT_OK
    assert capsys.readouterr().out.strip() == "ghs_http://127.0.0.1:9999"


def test_main_reports_a_mint_failure(tmp_path, pem, monkeypatch, capsys):
    for name, value in _env(tmp_path, pem).items():
        monkeypatch.setenv(name, value)

    def boom(_creds, *, api_url):
        raise GitHubAppError("GitHub said no")

    monkeypatch.setattr("witan_code.github_app.installation_token", boom)
    assert main([]) == EXIT_ERROR
    assert "GitHub said no" in capsys.readouterr().err


def test_main_rejects_an_unknown_argument():
    assert main(["--nope"]) == EXIT_ERROR
