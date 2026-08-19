"""`witan serve` re-serves the DEPLOYMENT's tools, and never silently the local store.

The defect these cover: `witan.config.load` reads a target's `server` field
only, so a target declaring `remote_url` and no `server` resolved `graph_uri`
to the default local store. `serve` then opened it — while the CLI, from the
same config in the same directory, dispatched to the deployment. An agent's
writes and its operator's `witan` commands went to two different graphs with
nothing to say so.

Pointed at an in-memory FastMCP server rather than a network, in the same shape
as test_remote_proxy.py.
"""

from __future__ import annotations

import asyncio
import io

import httpx2
import pytest
from fastmcp import Client, FastMCP
from fastmcp.tools import FunctionTool
from witan.config import RemoteConfig
from witan.remote.proxy import (
    RemoteCredentialRejected,
    RemoteServerProxy,
    RemoteUnreachable,
)
from witan.remote.serve import build_remote_server

REMOTE = RemoteConfig(url="http://unused/mcp", oidc_issuer="https://sso/realms/ol")


def _rejected_401() -> httpx2.HTTPStatusError:
    """What the transport raises when the deployment refuses the credential.

    Built as a real ``HTTPStatusError`` rather than raising
    ``RemoteCredentialRejected`` directly, because that is the shape the code
    under test classifies: ``auth_failure`` reads the status off the response,
    and a hand-thrown domain exception would be re-wrapped as
    ``RemoteUnreachable`` and quietly test nothing.
    """
    request = httpx2.Request("POST", REMOTE.url)
    response = httpx2.Response(401, request=request, text="unauthorized")
    return httpx2.HTTPStatusError(
        "Client error '401'", request=request, response=response
    )


def _backing() -> FastMCP:
    """A stand-in deployment: one work tool, one code tool, distinct schemas."""
    mcp: FastMCP = FastMCP("deployment")

    async def _echo(**kwargs):
        return {"saw": kwargs}

    mcp.add_tool(
        FunctionTool(
            name="task_ready",
            description="from the deployment",
            parameters={
                "type": "object",
                "properties": {"repo": {"type": "string"}},
            },
            fn=_echo,
        )
    )
    mcp.add_tool(
        FunctionTool(
            name="code_find_definition",
            description="served from the checkout, not the cluster",
            parameters={"type": "object", "properties": {}},
            fn=_echo,
        )
    )
    return mcp


@pytest.fixture
def proxy():
    backing = _backing()
    p = RemoteServerProxy(REMOTE, lambda: "tok")
    p._new_client = lambda _token: Client(backing)  # type: ignore[method-assign]
    return p


def _tools(server: FastMCP) -> dict:
    return {t.name: t for t in asyncio.run(server._list_tools())}


def test_the_deployments_tools_are_republished(proxy):
    served = _tools(asyncio.run(build_remote_server(REMOTE, proxy=proxy)))
    assert "task_ready" in served


def test_the_schema_comes_from_the_deployment_not_local_code(proxy):
    # The deployed release is the authority on its own surface; a locally
    # generated schema would drift from it at every version skew.
    served = _tools(asyncio.run(build_remote_server(REMOTE, proxy=proxy)))
    tool = served["task_ready"]
    assert tool.description == "from the deployment"
    assert sorted(tool.parameters["properties"]) == ["repo"]


def test_code_tools_are_not_republished(proxy):
    # witan-code has to run where the checkout is — indexing reads source
    # files. Its graph still belongs in the cluster, but via
    # `code_transport = "mcp"`, not by forwarding the tool call to a pod with
    # no checkout to read. Republishing would also shadow the local mount.
    served = _tools(asyncio.run(build_remote_server(REMOTE, proxy=proxy)))
    assert not [name for name in served if name.startswith("code_")]


def test_a_republished_call_reaches_the_deployment(proxy):
    server = asyncio.run(build_remote_server(REMOTE, proxy=proxy))

    async def _call():
        tool = await server.get_tool("task_ready")
        return await tool.run({"repo": "https://github.com/test/repo"})

    result = asyncio.run(_call())
    assert result.structured_content["saw"]["repo"] == "https://github.com/test/repo"


def test_an_unreachable_deployment_raises_rather_than_falling_back():
    # The whole point. Coming up on the local store here is the silent split
    # this module exists to prevent, so failing to list must stop the server.
    p = RemoteServerProxy(REMOTE, lambda: "tok")

    def _dead(_token):
        raise OSError("connection refused")

    p._new_client = _dead  # type: ignore[method-assign]
    with pytest.raises(RemoteUnreachable):
        asyncio.run(build_remote_server(REMOTE, proxy=p))


def test_serve_target_uses_the_local_server_when_no_remote_is_configured(monkeypatch):
    from witan import cli as cli_module
    from witan import config as cfg_module

    monkeypatch.setattr(cfg_module, "load_remote_config", lambda: None)
    import witan.server as srv

    assert cli_module._serve_target("stdio") is srv.mcp


def test_serve_target_exits_instead_of_serving_the_local_store(monkeypatch, capsys):
    from witan import cli as cli_module
    from witan import config as cfg_module

    monkeypatch.setattr(cfg_module, "load_remote_config", lambda: REMOTE)

    async def _boom(_remote):
        raise RemoteUnreachable("the deployment is not answering")

    monkeypatch.setattr("witan.remote.serve.build_remote_server", _boom)
    with pytest.raises(SystemExit):
        cli_module._serve_target("stdio")
    captured = capsys.readouterr()
    # ★ STDERR, NOT STDOUT. Under stdio transport stdout is the JSON-RPC
    # channel, so a diagnostic printed there is both a protocol violation and
    # invisible to the person it is written for.
    assert "does not fall back" in captured.err
    assert REMOTE.url in captured.err
    assert captured.out == ""


def test_the_startup_failure_names_the_endpoint_and_the_way_out():
    from witan.cli.remote_errors import remote_startup_failure

    msg = remote_startup_failure(REMOTE, RemoteUnreachable("nope"))
    # An MCP server's stderr is read, if ever, long after the fact — the
    # sentence has to carry the whole diagnosis on its own.
    assert REMOTE.url in msg
    assert "does not fall back" in msg
    assert "WITAN_TARGET=work" in msg


def test_a_deployed_target_is_not_served_over_a_socket(monkeypatch, capsys):
    # Every forwarded call carries the starting user's cached OIDC token and
    # nothing authenticates the caller on the way in, so binding this to a
    # port hands anyone who can reach it that user's identity.
    from witan import cli as cli_module
    from witan import config as cfg_module

    monkeypatch.setattr(cfg_module, "load_remote_config", lambda: REMOTE)
    with pytest.raises(SystemExit):
        cli_module._serve_target("streamable-http")
    captured = capsys.readouterr()
    assert "stdio-only" in captured.err
    assert REMOTE.url in captured.err


def test_the_stdio_refusal_names_the_credential_not_just_the_rule():
    from witan.cli.remote_errors import remote_serving_needs_stdio

    msg = remote_serving_needs_stdio(REMOTE, "streamable-http")
    # "stdio only" alone reads as arbitrary and invites a reverse proxy.
    assert "token" in msg
    assert REMOTE.url in msg


def test_the_code_graph_warning_survives_a_bracketed_target_name():
    # `where` renders as "target [production]", which Rich parses as a style
    # tag. Unescaped, the name is swallowed on render (or raises on a name Rich
    # cannot parse), so the warning names no target at all.
    from rich.console import Console
    from witan.cli import _local_code_graph_warning

    markup = _local_code_graph_warning("direct", "production")
    rendered = Console(file=io.StringIO(), width=200)
    rendered.print(markup)
    text = rendered.file.getvalue()
    assert "production" in text
    assert "direct" in text


def test_the_code_graph_warning_handles_no_target_at_all():
    from witan.cli import _local_code_graph_warning

    assert "config" in _local_code_graph_warning("direct", None)


def test_listing_tools_refreshes_once_on_a_rejected_credential():
    # `witan serve` lists at STARTUP, so a stale cache entry aborted the whole
    # server and told the user to log in when a refresh would have worked.
    # Listing writes nothing, so the no-retry rule for writes does not apply.
    backing = _backing()
    handed = []

    def _provider():
        handed.append("stale")
        return "stale"

    def _refresher(rejected):
        handed.append(f"refreshed-after:{rejected}")
        return "fresh"

    p = RemoteServerProxy(REMOTE, _provider, _refresher)

    def _client(token):
        if token == "stale":
            raise _rejected_401()
        return Client(backing)

    p._new_client = _client  # type: ignore[method-assign]
    tools = asyncio.run(p.remote_tools())
    assert {t.name for t in tools} == {"task_ready", "code_find_definition"}
    assert handed == ["stale", "refreshed-after:stale"]


def test_listing_tools_gives_up_when_there_is_nothing_to_refresh_with():
    # A pinned-token proxy (the concurrency probe does this deliberately) has
    # no refresher, so the rejection is the answer rather than a retry.
    p = RemoteServerProxy(REMOTE, lambda: "stale")

    def _client(_token):
        raise _rejected_401()

    p._new_client = _client  # type: ignore[method-assign]
    with pytest.raises(RemoteCredentialRejected):
        asyncio.run(p.remote_tools())
