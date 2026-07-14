"""RemoteServerProxy dispatches CLI calls over MCP (ADR 0005, path a).

Points the proxy at an in-memory FastMCP server (the real witan tools over a
throwaway omnigraph store) so argument mapping, result-shape parity, and
client-side repo resolution are exercised end to end without a network.
"""

from __future__ import annotations

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


def test_unknown_tool_is_refused(proxy):
    with pytest.raises(RemoteToolUnavailable):
        proxy.definitely_not_a_tool(repo="")
