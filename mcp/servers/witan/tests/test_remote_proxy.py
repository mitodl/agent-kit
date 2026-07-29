"""RemoteServerProxy dispatches CLI calls over MCP (ADR 0005, path a).

Points the proxy at an in-memory FastMCP server (the real witan tools over a
throwaway omnigraph store) so argument mapping, result-shape parity, and
client-side repo resolution are exercised end to end without a network.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client

from witan.config import RemoteConfig
from witan.remote.proxy import RemoteServerProxy, RemoteToolUnavailable

REPO = "https://github.com/test/repo"


@pytest.fixture
def proxy(server, monkeypatch):
    # `server` fixture wires witan.server.client to a fresh store.
    import witan.server as srv

    cfg = RemoteConfig(url="http://unused/mcp", oidc_issuer="https://sso/realms/ol")
    tokens: list[str] = []

    def _token() -> str:
        tokens.append("tok")
        return "tok"

    p = RemoteServerProxy(cfg, _token)
    monkeypatch.setattr(p, "_new_client", lambda _token: Client(srv.mcp))
    p._token_calls = tokens  # type: ignore[attr-defined]
    return p


def test_list_return_is_unwrapped_to_raw_list(proxy):
    # FastMCP wraps list returns as {"result": [...]}; .data unwraps it, so the
    # proxy hands back the same raw list an in-process call would.
    out = proxy.task_ready(repo="")
    assert isinstance(out, list)


def test_token_provider_is_called_per_invocation(proxy):
    proxy.task_ready(repo="")
    proxy.task_ready(repo="")
    assert proxy._token_calls == ["tok", "tok"]


def test_positional_first_arg_is_mapped_to_its_param_name(proxy):
    created = proxy.task_create(title="probe", description="d", repo=REPO)
    slug = created["slug"]
    # CLI calls s.task_get(slug) positionally; MCP is keyword-only.
    fetched = proxy.task_get(slug)
    assert fetched["slug"] == slug
    assert fetched["title"] == "probe"


def test_repo_none_is_resolved_client_side(proxy, monkeypatch):
    # The deployed server has no checkout: repo=None must become the client's
    # detected repo before the call is sent.
    monkeypatch.setattr(
        "witan.remote.proxy.repo_module.detect", lambda override=None: REPO
    )
    proxy.task_create(title="scoped", description="d", repo=REPO)
    # No repo arg at all → proxy injects the detected repo.
    ready = proxy.task_ready()
    assert any(t["repo"] == REPO for t in ready)


def test_repo_empty_string_sentinel_is_preserved(proxy, monkeypatch):
    # repo="" (all repos) must NOT be replaced by detection.
    monkeypatch.setattr(
        "witan.remote.proxy.repo_module.detect",
        lambda override=None: "https://other/repo",
    )
    captured = {}
    orig = proxy._map_args

    def spy(name, args, kwargs):
        result = orig(name, args, kwargs)
        captured[name] = result
        return result

    monkeypatch.setattr(proxy, "_map_args", spy)
    proxy.task_ready(repo="")
    assert captured["task_ready"]["repo"] == ""


def test_admin_only_functions_are_refused_without_network(proxy):
    for name in ("migrate_topics", "apply_schema", "merge_store"):
        with pytest.raises(RemoteToolUnavailable, match="in-cluster"):
            getattr(proxy, name)()


def test_admin_only_functions_are_not_registered_as_tools(server):
    """The server-side half of the admin refusal, and the load-bearing one.

    ``RemoteServerProxy._is_admin_tool`` runs in the *client*, so it is advisory
    — a stock MCP client with a valid JWT ignores it entirely. What actually
    keeps ``apply_schema``/``migrate_*``/``merge_store`` unreachable is that they
    are deliberately plain module functions, never ``@mcp.tool``. Assert that
    invariant here so a future decorator can't silently expose an admin op with
    no per-user identity to the whole deployment.
    """
    import witan.server as srv
    from witan.remote.proxy import _ADMIN_ONLY

    async def _list() -> set[str]:
        async with Client(srv.mcp) as client:
            return {t.name for t in await client.list_tools()}

    exposed = asyncio.run(_list())

    assert exposed, "expected the in-memory server to expose some tools"
    assert not (_ADMIN_ONLY & exposed)


def test_unknown_tool_is_refused(proxy):
    with pytest.raises(RemoteToolUnavailable):
        proxy.definitely_not_a_tool(repo="")


def test_srv_surfaces_misconfigured_remote_as_clean_exit(monkeypatch):
    # WITAN_REMOTE_URL without WITAN_OIDC_ISSUER makes load_remote_config raise
    # ValueError; _srv() must turn that into a clean SystemExit, not a traceback.
    from witan.cli import _common

    monkeypatch.setenv("WITAN_REMOTE_URL", "https://witan.example.org/mcp")
    monkeypatch.delenv("WITAN_OIDC_ISSUER", raising=False)
    monkeypatch.setattr(_common, "_server", None)
    with pytest.raises(SystemExit):
        _common._srv()
