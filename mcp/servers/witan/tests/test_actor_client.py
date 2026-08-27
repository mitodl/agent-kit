"""Unit tests for the per-request identity path (ADR 0004 follow-up).

Covers both halves: ``_resolve_client`` (which omnigraph client performs the
write) and ``_current_author`` (whose name the write is attributed to).

The ``_resolve_client`` tests need no omnigraph binary — ``OmnigraphClient``
construction is monkeypatched to a lightweight fake. The one end-to-end
attribution test uses the real ``server`` fixture and skips without the binary.
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
        def __init__(self, graph_uri, queries_dir, token, guard=None, graph_id=None):
            self.graph_uri = graph_uri
            self.graph_id = graph_id
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


def test_deployed_mode_refuses_without_access_token(deployed_mode, monkeypatch):
    """No JWT under a deployment means no caller — refuse, do not borrow.

    The fallback this replaces handed the request the module credential,
    which in every deployed environment is ``svc-witan-ci`` — an actor the
    memory Cedar bundle has no group for, so it could only ever come back
    ``unknown actor``. Refusing before the HTTP call distinguishes that from
    a genuinely missing actor-token entry, which produced the same message.
    """
    monkeypatch.setattr(srv, "get_access_token", lambda: None)
    with pytest.raises(RuntimeError, match="No authenticated actor"):
        srv._resolve_client()


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


def test_current_author_local_mode_ignores_jwt(local_mode, monkeypatch):
    monkeypatch.setattr(srv, "cfg", srv.cfg.model_copy(update={"author": "Local Dev"}))
    monkeypatch.setattr(
        srv,
        "get_access_token",
        lambda: _FakeToken({"sub": "alice", "preferred_username": "alice"}),
    )
    assert srv._current_author() == "Local Dev"


def test_current_author_deployed_without_token_falls_back_to_config(
    deployed_mode, monkeypatch
):
    """Attribution degrades to the configured author with no caller identity.

    Unreachable in practice under a deployment now that ``_resolve_client``
    refuses the same condition, but the two are independent functions and
    this one has no store call to fail closed on.
    """
    monkeypatch.setattr(srv, "cfg", srv.cfg.model_copy(update={"author": "witan-svc"}))
    monkeypatch.setattr(srv, "get_access_token", lambda: None)
    assert srv._current_author() == "witan-svc"


def test_current_author_prefers_preferred_username(deployed_mode, monkeypatch):
    monkeypatch.setattr(
        srv,
        "get_access_token",
        lambda: _FakeToken(
            {"sub": "uuid-1", "preferred_username": "tmacey", "email": "t@example.org"}
        ),
    )
    assert srv._current_author() == "tmacey"


def test_current_author_falls_back_to_email(deployed_mode, monkeypatch):
    monkeypatch.setattr(
        srv,
        "get_access_token",
        lambda: _FakeToken({"sub": "uuid-1", "email": "t@example.org"}),
    )
    assert srv._current_author() == "t@example.org"


@pytest.mark.parametrize("blank", ["", "   "])
def test_current_author_skips_blank_display_claims(deployed_mode, monkeypatch, blank):
    """A present-but-empty claim must not win over a usable one below it."""
    monkeypatch.setattr(
        srv,
        "get_access_token",
        lambda: _FakeToken(
            {"sub": "uuid-1", "preferred_username": blank, "email": "t@example.org"}
        ),
    )
    assert srv._current_author() == "t@example.org"


def test_current_author_skips_non_string_display_claims(deployed_mode, monkeypatch):
    monkeypatch.setattr(
        srv,
        "get_access_token",
        lambda: _FakeToken({"sub": "uuid-1", "preferred_username": 42}),
    )
    assert srv._current_author() == "act-uuid-1"


def test_current_author_last_resort_is_the_derived_actor_id(deployed_mode, monkeypatch):
    """Same id the token-mapping layer uses — opaque, but never the wrong user."""
    monkeypatch.setattr(srv, "get_access_token", lambda: _FakeToken({"sub": "Alice"}))
    assert srv._current_author() == "act-alice"


def test_current_author_trims_whitespace(deployed_mode, monkeypatch):
    monkeypatch.setattr(
        srv,
        "get_access_token",
        lambda: _FakeToken({"sub": "uuid-1", "preferred_username": "  tmacey  "}),
    )
    assert srv._current_author() == "tmacey"


def test_deployed_memory_write_is_attributed_to_the_caller(
    server, deployed_mode, monkeypatch
):
    """End-to-end: the stored node carries the JWT's user, not the server's config."""
    monkeypatch.setattr(
        srv,
        "get_access_token",
        lambda: _FakeToken({"sub": "uuid-1", "preferred_username": "tmacey"}),
    )

    stored = server.memory_store(kind="lesson", title="attributed", content="body")
    rows = srv.client.read("read.gq", "get_memory", {"slug": stored["slug"]})

    assert rows[0]["author"] == "tmacey"


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
