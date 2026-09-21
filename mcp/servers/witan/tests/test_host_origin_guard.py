"""Host/Origin validation on the HTTP transport.

`witan serve`'s loopback endpoint has no authentication — over stdio the only
client is the agent harness that spawned the process, and a local
`--transport streamable-http` inherits that assumption without inheriting the
boundary that made it safe. Once `witan ui` serves a page from the same
process, the browser is a client too, and any OTHER page the user has open can
POST tool calls to 127.0.0.1 and read the graph.

fastmcp ships the guard and leaves it off (`fastmcp/settings.py:280`), so what
is pinned here is in two halves, and only the pair is worth anything:

  * `serve` asks for it, with no allowlist (the wiring, which is the change);
  * fastmcp resolves that into no allowlist for the deployment's non-loopback
    bind (the step between the two, which nothing else here reaches);
  * the app then rejects the two attacks and admits the four legitimate
    request shapes (the behaviour, which a fastmcp bump could reverse).

THE DEPLOYED SHAPE IS A TEST AND NOT A FOOTNOTE. An allowlist would also turn
Origin validation on unconditionally, and behind APISIX's TLS termination the
server computes the request origin as `http://<host>` while the page sends
`Origin: https://<host>` — every POST from the deployed page 403s. The
`witan.example` cases below are that page's request.
"""

from functools import partial

import pytest
from starlette.testclient import TestClient

from witan import server as srv

# Enough of an MCP request to reach a real status code rather than a parse
# error, so an "allowed" assertion is a request that actually landed.
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


def _guarded_app():
    """witan's real ASGI app, with the protection `serve` now asks for."""
    return srv.mcp.http_app(path="/mcp", host_origin_protection="auto")


@pytest.fixture
def loopback():
    """`witan ui` / a local `serve`: scope["server"] is 127.0.0.1."""
    with TestClient(_guarded_app(), base_url="http://127.0.0.1:8000") as client:
        yield client


@pytest.fixture
def deployed():
    """The deployed pod: a non-loopback server, reached by hostname.

    The guard branches on whether `scope["server"]` is loopback. Real uvicorn
    puts an IP there (the accepted connection's own local address), where
    TestClient puts the hostname from `base_url`; both are non-loopback, so
    the branch taken is the deployment's. What this does NOT cover is
    reaching a deployed pod over its own loopback, e.g. `kubectl
    port-forward`, which flips the guard into loopback mode. That still works
    (`Host: localhost:<port>` is in `DEFAULT_HOSTS`) but it is a different
    path from the one below.
    """
    with TestClient(_guarded_app(), base_url="http://witan.example") as client:
        yield client


def test_a_foreign_host_is_misdirected(loopback):
    """DNS rebinding: a name the attacker controls, resolved to 127.0.0.1."""
    response = loopback.post(
        "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "host": "evil.example"}
    )

    assert response.status_code == 421


def test_a_foreign_origin_is_forbidden(loopback):
    """The cross-site POST: another page the user has open, calling tools."""
    response = loopback.post(
        "/mcp",
        json=INITIALIZE,
        headers={**MCP_HEADERS, "origin": "https://evil.example"},
    )

    assert response.status_code == 403


def test_the_pages_own_origin_is_allowed(loopback):
    """`witan ui` serves the bundle from this origin; it must reach /mcp."""
    response = loopback.post(
        "/mcp",
        json=INITIALIZE,
        headers={**MCP_HEADERS, "origin": "http://127.0.0.1:8000"},
    )

    assert response.status_code == 200


def test_a_request_with_no_origin_is_allowed(loopback):
    """The CLI, curl and every agent send no Origin, and are not browsers."""
    response = loopback.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)

    assert response.status_code == 200


def test_the_deployed_page_reaches_mcp_over_https(deployed):
    """The case an allowlist would break: https Origin, http request origin."""
    response = deployed.post(
        "/mcp",
        json=INITIALIZE,
        headers={**MCP_HEADERS, "origin": "https://witan.example"},
    )

    assert response.status_code == 200


def test_the_kubelet_probe_still_passes(deployed):
    """`/health` is behind the same app-wide middleware as `/mcp`.

    The probe carries no Origin and a Host the guard never validates off
    loopback. If either stopped being true the pod would never go Ready.
    """
    response = deployed.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_a_non_loopback_bind_resolves_to_no_allowlist():
    """★ THE ASSERTION THAT PROTECTS THE DEPLOYMENT, and the only one here that
    exercises the step between `run` and the app the other tests build.

    fastmcp resolves an allowlist of its own before constructing the
    middleware: for a LOOPBACK bind it appends the bind host, so the middleware
    does run with `allowed_hosts=["127.0.0.1"]` and the CLI comment's "no
    allowlist" is about what `serve` passes, not about what the middleware
    receives. For a non-loopback bind it must resolve to `None`, because an
    allowlist there switches Origin validation on unconditionally and 403s
    every POST from the deployed page.

    If a future fastmcp started injecting one for a non-loopback bind, the
    deployment would break and every other test in this file would still pass:
    they construct the app directly and never reach this function.
    """
    from fastmcp.server.mixins.transport import _resolve_allowed_hosts_for_run

    resolve = partial(
        _resolve_allowed_hosts_for_run,
        host_origin_protection="auto",
        allowed_hosts=None,
        configured_allowed_hosts=None,
    )

    assert resolve(host="0.0.0.0") is None  # noqa: S104 — the deployment's bind
    assert resolve(host="127.0.0.1") == ["127.0.0.1"]


def test_serve_turns_the_guard_on_with_no_allowlist(monkeypatch):
    """The wiring. Everything above tests an app the CLI would have to build.

    Asserting the allowlists are absent is not padding: passing either one is
    what switches Origin validation on for the deployment and 403s its page,
    and `None` is not interchangeable with `[]` — an empty list still counts
    as explicit (`fastmcp/server/http.py:241`).
    """
    from fastmcp import FastMCP

    from witan import cli

    recorded: dict = {}
    stub = FastMCP("guard-wiring-probe")
    monkeypatch.setattr(stub, "run", lambda **kw: recorded.update(kw))
    monkeypatch.setattr(cli, "_serve_target", lambda transport: stub)

    cli.serve(transport="streamable-http")

    assert recorded["host_origin_protection"] == "auto"
    assert "allowed_hosts" not in recorded
    assert "allowed_origins" not in recorded
