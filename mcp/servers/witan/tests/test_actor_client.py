"""Unit tests for the per-actor OmnigraphClient resolution path (ADR 0004 follow-up).

No omnigraph binary required — ``OmnigraphClient`` construction inside
``_resolve_client`` is monkeypatched to a lightweight fake.
"""

import pytest

import witan.server as srv
from witan.config import IdentityConfig


class _FakeToken:
    def __init__(self, claims):
        self.claims = claims


@pytest.fixture(autouse=True)
def _clean_actor_cache():
    """Every test starts and ends with an empty per-actor client cache."""
    srv._actor_clients.clear()
    yield
    srv._actor_clients.clear()


@pytest.fixture
def local_mode(monkeypatch):
    monkeypatch.setattr(
        srv,
        "identity_cfg",
        IdentityConfig(oidc_issuer=None, oidc_audience=None, actor_tokens_file=None),
    )


@pytest.fixture
def deployed_mode(monkeypatch):
    monkeypatch.setattr(
        srv,
        "identity_cfg",
        IdentityConfig(
            oidc_issuer="https://sso.example.org/realms/witan",
            oidc_audience="witan",
            actor_tokens_file="/dev/null",
        ),
    )

    class _FakeResolver:
        def __init__(self, tokens):
            self.tokens = tokens
            self.calls = []

        def resolve(self, actor_id):
            self.calls.append(actor_id)
            if actor_id not in self.tokens:
                raise LookupError(
                    f"No omnigraph bearer token provisioned for {actor_id!r}"
                )
            return self.tokens[actor_id]

    resolver = _FakeResolver({"act-alice": "tok-alice", "act-bob": "tok-bob"})
    monkeypatch.setattr(srv, "actor_token_resolver", resolver)

    built = []

    class _FakeOmnigraphClient:
        def __init__(self, graph_uri, queries_dir, token, guard=None):
            self.graph_uri = graph_uri
            self.queries_dir = queries_dir
            self.token = token
            self.guard = guard
            built.append(self)

    monkeypatch.setattr(srv, "OmnigraphClient", _FakeOmnigraphClient)
    return resolver, built


def test_local_mode_ignores_access_token_and_returns_default(local_mode, monkeypatch):
    monkeypatch.setattr(srv, "get_access_token", lambda: _FakeToken({"sub": "alice"}))
    assert srv._resolve_client() is srv._default_client
    assert srv._resolve_client() is srv._default_client


def test_deployed_mode_falls_back_to_default_without_access_token(
    deployed_mode, monkeypatch
):
    monkeypatch.setattr(srv, "get_access_token", lambda: None)
    assert srv._resolve_client() is srv._default_client


def test_deployed_mode_builds_per_actor_client_from_sub_claim(
    deployed_mode, monkeypatch
):
    resolver, built = deployed_mode
    monkeypatch.setattr(srv, "get_access_token", lambda: _FakeToken({"sub": "Alice"}))

    result = srv._resolve_client()

    assert result.token == "tok-alice"
    assert resolver.calls == ["act-alice"]
    assert len(built) == 1


def test_deployed_mode_caches_client_across_calls(deployed_mode, monkeypatch):
    resolver, built = deployed_mode
    monkeypatch.setattr(srv, "get_access_token", lambda: _FakeToken({"sub": "alice"}))

    first = srv._resolve_client()
    second = srv._resolve_client()

    assert first is second
    assert resolver.calls == ["act-alice"]  # resolved (and constructed) only once
    assert len(built) == 1


def test_deployed_mode_gives_different_actors_different_clients(
    deployed_mode, monkeypatch
):
    resolver, built = deployed_mode
    tokens = iter([_FakeToken({"sub": "alice"}), _FakeToken({"sub": "bob"})])
    monkeypatch.setattr(srv, "get_access_token", lambda: next(tokens))

    alice_client = srv._resolve_client()
    bob_client = srv._resolve_client()

    assert alice_client is not bob_client
    assert alice_client.token == "tok-alice"
    assert bob_client.token == "tok-bob"
    assert len(built) == 2


def test_deployed_mode_unprovisioned_actor_raises(deployed_mode, monkeypatch):
    monkeypatch.setattr(srv, "get_access_token", lambda: _FakeToken({"sub": "carol"}))
    with pytest.raises(LookupError, match="act-carol"):
        srv._resolve_client()


def test_actor_scoped_client_proxy_delegates_every_attribute(monkeypatch):
    class _Sentinel:
        graph_uri = "s3://sentinel"

        def read(self, *a, **kw):
            return ["sentinel-row"]

    sentinel = _Sentinel()
    monkeypatch.setattr(srv, "_resolve_client", lambda: sentinel)

    assert isinstance(srv.client, srv._ActorScopedClient)
    assert srv.client.graph_uri == "s3://sentinel"
    assert srv.client.read() == ["sentinel-row"]
