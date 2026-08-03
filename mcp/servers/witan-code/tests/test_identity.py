"""Who witan-code writes as — the identity that owns its branch views.

Not `$USER`: a local username is not what the cluster's bearer tokens and
Cedar policies are written against, and two people can share one.
"""

import base64
import json

import pytest

from witan_code import identity


def _token(sub: str) -> str:
    """A JWT-shaped string — only the payload segment is ever read."""
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).decode()
    return f"header.{payload.rstrip('=')}.signature"


@pytest.fixture
def config(tmp_path, monkeypatch):
    """An isolated config.toml + token cache, with nothing inherited."""
    monkeypatch.setenv("WITAN_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("WITAN_TOKEN_CACHE", str(tmp_path / "tokens.json"))
    monkeypatch.delenv(identity.ACTOR_ENV_VAR, raising=False)
    monkeypatch.delenv("WITAN_REMOTE_URL", raising=False)
    monkeypatch.delenv("WITAN_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    return tmp_path


def _login(config_dir, sub: str, *, issuer: str, client_id: str = "witan-cli"):
    (config_dir / "tokens.json").write_text(
        json.dumps(
            {
                f"{issuer}|{client_id}": {
                    "access_token": _token(sub),
                    "expires_at": 0,  # deliberately expired — see below
                }
            }
        )
    )


def test_no_deployment_means_no_actor(config):
    """Purely local use: a local store has one user, who is its writer, and
    its view names stay un-namespaced. Indexing offline must not need a login.
    """
    assert identity.actor_id() is None


def test_a_configured_deployment_without_a_login_has_no_actor(config, monkeypatch):
    """The write guard turns this into a refusal with a login hint rather than
    letting an un-owned view land on the shared graph."""
    monkeypatch.setenv("WITAN_REMOTE_URL", "https://witan.example.org/mcp")
    monkeypatch.setenv("WITAN_OIDC_ISSUER", "https://sso/realms/ol")

    assert identity.actor_id() is None


def test_the_actor_comes_from_the_cached_oidc_session(config, monkeypatch):
    issuer = "https://sso/realms/ol"
    monkeypatch.setenv("WITAN_REMOTE_URL", "https://witan.example.org/mcp")
    monkeypatch.setenv("WITAN_OIDC_ISSUER", issuer)
    _login(config, "f47ac10b-58cc-4372-a567-0e02b2c3d479", issuer=issuer)

    assert identity.actor_id() == "act-f47ac10b-58cc-4372-a567-0e02b2c3d479"


def test_an_expired_token_still_names_its_owner(config, monkeypatch):
    """Identity is not authorization. Who you are does not change when a token
    expires, and resolving it must not block on a network refresh — the
    cached entry written above is already past its `expires_at`.
    """
    issuer = "https://sso/realms/ol"
    monkeypatch.setenv("WITAN_REMOTE_URL", "https://witan.example.org/mcp")
    monkeypatch.setenv("WITAN_OIDC_ISSUER", issuer)
    _login(config, "alice", issuer=issuer)

    assert identity.actor_id() == "act-alice"


def test_the_env_var_wins_over_the_session(config, monkeypatch):
    """For a writer with a cluster token and no interactive login — CI, a
    maintenance job."""
    issuer = "https://sso/realms/ol"
    monkeypatch.setenv("WITAN_REMOTE_URL", "https://witan.example.org/mcp")
    monkeypatch.setenv("WITAN_OIDC_ISSUER", issuer)
    _login(config, "alice", issuer=issuer)
    monkeypatch.setenv(identity.ACTOR_ENV_VAR, "act-ci-indexer")

    assert identity.actor_id() == "act-ci-indexer"


def test_a_raw_sub_in_the_env_var_is_derived(config, monkeypatch):
    monkeypatch.setenv(identity.ACTOR_ENV_VAR, "f47ac10b-58cc")

    assert identity.actor_id() == "act-f47ac10b-58cc"


def test_a_malformed_actor_id_raises_rather_than_being_rewritten(config, monkeypatch):
    """Silently re-deriving would namespace this process's views under an id
    nobody authorized it for — writes that succeed locally and are refused by
    the cluster."""
    monkeypatch.setenv(identity.ACTOR_ENV_VAR, "act-Alice Smith")

    with pytest.raises(ValueError, match="not a valid actor id"):
        identity.actor_id()


def test_a_target_block_can_carry_the_actor(config, monkeypatch):
    (config / "config.toml").write_text(
        "[targets.prod]\n"
        'remote_url = "https://witan.example.org/mcp"\n'
        'oidc_issuer = "https://sso/realms/ol"\n'
        'actor = "act-from-target"\n'
    )
    monkeypatch.setenv("WITAN_TARGET", "prod")

    assert identity.actor_id() == "act-from-target"


def test_the_identity_is_resolved_once_per_process(config, monkeypatch):
    """A witan-code process writes as exactly one identity for its lifetime;
    re-resolving mid-process would leave views written under the old id."""
    monkeypatch.setenv(identity.ACTOR_ENV_VAR, "act-alice")
    assert identity.actor_id() == "act-alice"

    monkeypatch.setenv(identity.ACTOR_ENV_VAR, "act-bob")
    assert identity.actor_id() == "act-alice"

    identity.reset_cache()
    assert identity.actor_id() == "act-bob"


def test_witan_and_witan_code_derive_the_same_id():
    """A view named by one derivation and authorized against another is a bug
    with no symptom until two users collide."""
    from witan_core.identity import derive_actor_id

    monkey_sub = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    assert derive_actor_id(monkey_sub) == f"act-{monkey_sub}"
