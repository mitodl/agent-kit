"""The web UI's token may call the UI's bound reads and nothing else.

Driven over real HTTP with Keycloak-shaped JWTs rather than by calling the
check with a hand-built ``AccessToken``, because the gate rests on fastmcp
reading Keycloak's ``azp`` claim into ``AccessToken.client_id``. A fastmcp
release that took the client from somewhere else would leave a check that
passes its unit test and lets every UI token write.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from fastmcp.server.middleware import AuthMiddleware
from fastmcp.utilities.tests import asgi_client

from witan import server as srv
from witan import ui_access
from witan.remote.proxy import _READ_ONLY

ISSUER = "https://sso.example.invalid/realms/ol-platform-engineering"
UI_CLIENT = "witan-ui"
KEYS = RSAKeyPair.generate()


def _guarded() -> FastMCP:
    mcp = FastMCP(
        "witan-ui-access-probe",
        auth=JWTVerifier(public_key=KEYS.public_key, issuer=ISSUER, audience="witan"),
    )
    mcp.add_middleware(AuthMiddleware(auth=ui_access.reads_only_for(UI_CLIENT)))

    @mcp.tool
    def task_get(slug: str) -> str:
        return f"read {slug}"

    @mcp.tool
    def task_create(title: str) -> str:
        return f"created {title}"

    return mcp


def _token(azp: str) -> str:
    """A token shaped like Keycloak's: ``aud`` from the audience mapper, ``azp``
    naming the client it was issued to."""
    return KEYS.create_token(
        subject="f2b7c1e0-0000-0000-0000-000000000000",
        issuer=ISSUER,
        audience=["witan", "account"],
        additional_claims={"azp": azp, "preferred_username": "someone@example"},
    )


async def _list_and_call(azp: str, tool: str, args: dict) -> tuple[set[str], str]:
    async with asgi_client(_guarded(), auth=_token(azp)) as client:
        listed = {t.name for t in await client.list_tools()}
        result = await client.call_tool(tool, args)
        return listed, result.data


def test_the_ui_token_reads():
    listed, data = asyncio.run(_list_and_call(UI_CLIENT, "task_get", {"slug": "x"}))
    assert data == "read x"
    assert listed == {"task_get"}


def test_the_ui_token_cannot_write():
    with pytest.raises(ToolError, match="Authorization failed for tool 'task_create'"):
        asyncio.run(_list_and_call(UI_CLIENT, "task_create", {"title": "x"}))


@pytest.mark.parametrize("azp", ["witan-cli", "witan-desktop"])
def test_every_other_client_still_writes(azp):
    listed, data = asyncio.run(_list_and_call(azp, "task_create", {"title": "x"}))
    assert data == "created x"
    assert listed == {"task_get", "task_create"}


def test_with_no_ui_client_configured_the_gate_is_off():
    check = ui_access.reads_only_for(None)
    mcp = _guarded()
    mcp.middleware[-1] = AuthMiddleware(auth=check)

    async def go():
        async with asgi_client(mcp, auth=_token(UI_CLIENT)) as client:
            return (await client.call_tool("task_create", {"title": "x"})).data

    assert asyncio.run(go()) == "created x"


def test_the_server_registers_the_gate():
    assert any(isinstance(m, AuthMiddleware) for m in srv.mcp.middleware)


def test_the_bound_set_holds_only_reads():
    """A write in ``READ_TOOLS`` would hand it straight back to the UI token.

    Checked against the remote proxy's read-only list, which defaults an
    unclassified tool to "it wrote". witan-code's optional three are not
    witan's to classify there.
    """
    assert ui_access.READ_TOOLS - ui_access.OPTIONAL_READ_TOOLS <= _READ_ONLY
