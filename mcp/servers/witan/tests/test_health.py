"""The `/health` endpoint a deployed witan is probed on.

Three properties, each of which has a matching production failure if it breaks:

  * it answers WITHOUT a bearer token — the kubelet has none, so an
    authenticated probe means a pod that is never Ready;
  * the MCP endpoint stays authenticated — the exemption must be the one route,
    not a hole punched through the auth provider;
  * it NEVER touches the graph — a deep probe converts backend slowness into a
    liveness kill, which is exactly the outage documented on the handler.
"""

import pytest
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.testclient import TestClient

from witan import server as srv


@pytest.fixture
def http_client():
    """A test client over witan's real ASGI app, auth provider and all."""
    with TestClient(srv.mcp.http_app(path="/mcp")) as client:
        yield client


def test_health_answers_without_a_bearer_token(http_client):
    """The kubelet carries no credential; an authenticated probe never passes."""
    response = http_client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["service"] == "witan"


def test_the_exemption_is_the_route_and_not_the_auth_provider():
    """`/health` is open BECAUSE it is a custom route, not because auth is off.

    witan builds `_jwt_verifier` from `WITAN_OIDC_ISSUER`, which is unset under
    test — so the `http_client` fixture's server has no auth provider at all,
    and asserting anything about `/mcp` there proves nothing. (An earlier
    version of this test did exactly that and "passed" against a 400.)

    So the guarded case is built explicitly: witan's own handler, mounted on a
    FastMCP carrying a verifier that can never succeed. `/health` must answer
    while `/mcp` refuses. What this pins is the fastmcp invariant the
    deployment rests on — `auth=` guards the protocol endpoint, not the whole
    ASGI app — which is the thing a version bump could silently reverse,
    leaving every probe 401 and the pod permanently un-Ready.
    """
    guarded = FastMCP(
        "witan-auth-scope-probe",
        auth=JWTVerifier(
            jwks_uri="https://example.invalid/jwks",
            issuer="https://example.invalid/realm",
            audience="witan",
        ),
    )
    guarded.custom_route("/health", methods=["GET"])(srv.health)

    with TestClient(guarded.http_app(path="/mcp")) as client:
        assert client.get("/health").status_code == 200
        assert (
            client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Accept": "application/json, text/event-stream"},
            ).status_code
            == 401
        )


def test_health_never_touches_the_graph(monkeypatch, http_client):
    """★ The property that keeps a slow backend from becoming a dead pod.

    A saturated graph is precisely when the probe fires and precisely when
    killing the pod does the most harm — so "healthy" here must mean "this
    process is running", never "the data tier answered". Enforced by making
    ANY use of the module-level client explode: if a future edit adds a
    convenience lookup to the handler, this test fails instead of the service.
    """

    class ExplodingClient:
        def __getattr__(self, name):
            msg = (
                f"/health touched the graph (client.{name}) — see the handler's "
                "docstring: a deep probe turns backend slowness into a liveness kill"
            )
            raise AssertionError(msg)

    monkeypatch.setattr(srv, "client", ExplodingClient())

    assert http_client.get("/health").status_code == 200


def test_health_reports_a_version(http_client):
    """So a rollout is verifiable with a curl rather than an exec into the pod."""
    version = http_client.get("/health").json()["version"]

    assert version
    assert version != "unknown"
