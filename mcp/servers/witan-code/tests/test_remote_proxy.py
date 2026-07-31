"""RemoteServerProxy dispatches witan-code CLI reads over MCP (ADR 0005, path a).

Points the proxy at an in-memory FastMCP server (the real ``code_*`` tools over
a throwaway store built from ``sample_repo``) so argument mapping, result-shape
parity, and the local-only refusals are exercised end to end without a network.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client
from witan_core.remote.config import RemoteConfig

from witan_code.remote.proxy import RemoteServerProxy, RemoteToolUnavailable

from .conftest import requires_stack

REPO = "https://github.com/test/cg"


@pytest.fixture
def proxy(sample_repo, monkeypatch):
    """A proxy wired to an in-memory server holding a freshly indexed repo."""
    from witan_code import config as cfg_mod
    from witan_code import server as srv

    # server.cfg was captured at import; refresh it for the test's env + store.
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()
    srv._git_context.clear()
    _fn(srv.code_reindex)(path=str(sample_repo))

    cfg = RemoteConfig(url="http://unused/mcp", oidc_issuer="https://sso/realms/ol")
    tokens: list[str] = []

    def _token() -> str:
        tokens.append("tok")
        return "tok"

    p = RemoteServerProxy(cfg, _token)
    monkeypatch.setattr(p, "_new_client", lambda _token: Client(srv.mcp))
    p._token_calls = tokens  # type: ignore[attr-defined]
    return p


def _fn(tool):
    import inspect

    fn = getattr(tool, "fn", tool)
    if inspect.iscoroutinefunction(fn):
        return lambda *a, **kw: asyncio.run(fn(*a, **kw))
    return fn


@requires_stack
def test_list_return_is_unwrapped_to_raw_list(proxy):
    # FastMCP wraps list returns as {"result": [...]}; .data unwraps it, so the
    # proxy hands back the same raw list an in-process call would.
    out = proxy.code_indexed_repos()
    assert isinstance(out, list)
    assert {r["repo"] for r in out} == {REPO}


@requires_stack
def test_token_provider_is_called_per_invocation(proxy):
    proxy.code_indexed_repos()
    proxy.code_indexed_repos()
    assert proxy._token_calls == ["tok", "tok"]


@requires_stack
def test_positional_first_arg_is_mapped_to_its_param_name(proxy):
    # CLI-style call sites pass the first argument positionally; MCP is
    # keyword-only, so the proxy maps it via the tool's input schema.
    hits = proxy.code_search_symbol("helper")
    assert any(h["name"] == "helper" for h in hits)


@requires_stack
def test_repo_none_is_not_rewritten_client_side(proxy, monkeypatch):
    """The divergence from witan-council's proxy, pinned.

    There, ``repo=None`` means "detect the current repo" on every tool, so the
    client substitutes a value. Here it means "every indexed repo" on the
    bridge-wide tools — injecting a detected repo would silently narrow
    ``witan-code stitch`` from the whole store to one repo.
    """
    captured: dict[str, dict] = {}
    orig = proxy._map_args

    def spy(name, args, kwargs):
        result = orig(name, args, kwargs)
        captured[name] = result
        return result

    monkeypatch.setattr(proxy, "_map_args", spy)
    proxy.code_unresolved_symbols(repo=None)
    assert "repo" not in captured["code_unresolved_symbols"]


@requires_stack
def test_repo_tools_round_trip_through_the_proxy(proxy):
    # The four tools the CLI's read commands added, reachable remotely.
    assert isinstance(proxy.code_repo_symbols(repo=REPO), list)
    assert isinstance(proxy.code_unresolved_symbols(), list)
    assert isinstance(proxy.code_precise_edges(), list)
    deps = proxy.code_repo_dependencies()
    assert set(deps) == {"repos", "edges"}
    branches = proxy.code_indexed_branches()
    assert [b["repo"] for b in branches] == [REPO]
    assert "main" in branches[0]["branches"]


def test_local_only_tools_are_refused_without_network():
    cfg = RemoteConfig(url="http://unused/mcp", oidc_issuer="https://sso/realms/ol")
    proxy = RemoteServerProxy(cfg, lambda: "tok")
    with pytest.raises(RemoteToolUnavailable, match="witan-code index"):
        proxy.code_reindex()


@requires_stack
def test_unknown_tool_is_refused(proxy):
    with pytest.raises(RemoteToolUnavailable):
        proxy.definitely_not_a_tool()


def test_srv_surfaces_misconfigured_remote_as_clean_exit(monkeypatch, tmp_path):
    # WITAN_REMOTE_URL without WITAN_OIDC_ISSUER makes load_remote_config raise
    # ValueError; _srv() must turn that into a clean SystemExit, not a traceback.
    from witan_code import cli as cli_module

    monkeypatch.setenv("WITAN_CONFIG", str(tmp_path / "unused.toml"))
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.setenv("WITAN_REMOTE_URL", "https://witan.example.org/mcp")
    monkeypatch.delenv("WITAN_OIDC_ISSUER", raising=False)
    monkeypatch.setattr(cli_module, "_server", None)
    with pytest.raises(SystemExit):
        cli_module._srv()


def test_srv_returns_the_proxy_when_a_remote_is_configured(monkeypatch, tmp_path):
    from witan_code import cli as cli_module

    monkeypatch.setenv("WITAN_CONFIG", str(tmp_path / "unused.toml"))
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.setenv("WITAN_REMOTE_URL", "https://witan.example.org/mcp")
    monkeypatch.setenv("WITAN_OIDC_ISSUER", "https://sso.example.org/realms/ol")
    monkeypatch.setattr(cli_module, "_server", None)

    assert isinstance(cli_module._srv(), RemoteServerProxy)
    assert cli_module._is_remote() is True
    cli_module._server = None


def test_srv_is_the_server_module_by_default(monkeypatch, tmp_path):
    from witan_code import cli as cli_module
    from witan_code import server as server_module

    monkeypatch.setenv("WITAN_CONFIG", str(tmp_path / "unused.toml"))
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.delenv("WITAN_REMOTE_URL", raising=False)
    monkeypatch.setattr(cli_module, "_server", None)

    assert cli_module._srv() is server_module
    assert cli_module._is_remote() is False
    cli_module._server = None
