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
def test_keyword_args_reach_the_server(proxy):
    hits = proxy.code_search_symbol(query="helper")
    assert any(h["name"] == "helper" for h in hits)


@requires_stack
def test_positional_arg_is_refused(proxy):
    # MCP is keyword-only on the wire. The proxy used to map positionals onto
    # the input schema's property order, which is unordered by specification —
    # see witan_core.remote.proxy's module docstring for what that misbound.
    with pytest.raises(RemoteToolUnavailable, match="by keyword"):
        proxy.code_search_symbol("helper")


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
    assert branches[0]["views"] == []


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


# ── an unreachable deployment ─────────────────────────────────────────────
# The classification itself is pinned in witan-core; what witan-code owns is
# the wording, and the fact that `cli()` guards at all — it had no try/except,
# so a deployment that was down printed a traceback out of `witan-code symbols`.


def _dead(**cfg_kwargs) -> str:
    cfg = RemoteConfig(
        url="https://witan.example.org/mcp",
        oidc_issuer="https://sso/realms/ol",
        **cfg_kwargs,
    )
    proxy = RemoteServerProxy(cfg, lambda: "tok")
    return proxy._unreachable_error(RuntimeError("All connection attempts failed"))


def test_unreachable_message_names_the_endpoint_and_the_cause():
    message = _dead()
    assert "https://witan.example.org/mcp" in message
    assert "All connection attempts failed" in message


def test_unreachable_message_states_that_there_is_no_fallback():
    # Silently answering from a local index is worse here than failing: an
    # index that is stale or absent returns no hits, which reads exactly like a
    # true "nothing calls this".
    assert "does not fall back" in _dead()


def test_unreachable_message_names_remote_url_not_code_transport():
    # `remote_url` is what routes a caller to this proxy. `code_transport`
    # selects the direct-omnigraph store path, and naming it here would send
    # the reader to unset a setting that is not in play.
    message = _dead(url_source="`WITAN_REMOTE_URL`")
    assert "`WITAN_REMOTE_URL`" in message
    assert "code_transport" not in message


def test_unreachable_message_names_the_setting_that_supplied_the_url():
    assert "`remote_url` on target [qa]" in _dead(
        target_name="qa", url_source="`remote_url` on target [qa]"
    )


def test_unreachable_message_does_not_infer_the_setting_from_the_target():
    # A matched target does not mean the target supplied the URL — env
    # overrides it while leaving `target_name` set. Inferring would name a
    # key that is overridden, and the caller would still be routed remotely.
    message = _dead(target_name="qa", url_source="`WITAN_REMOTE_URL`")
    assert "`WITAN_REMOTE_URL`" in message
    assert "target [qa]" not in message


def test_cli_prints_an_unreachable_remote_instead_of_a_traceback(monkeypatch, capsys):
    from types import SimpleNamespace

    from witan_code import cli as cli_module
    from witan_code.remote.proxy import RemoteUnreachable

    def _down():
        raise RemoteUnreachable("witan-code is down at X")

    monkeypatch.setattr(cli_module, "app", SimpleNamespace(meta=_down))
    with pytest.raises(SystemExit) as exit_code:
        cli_module.cli()
    assert exit_code.value.code == 1
    assert "witan-code is down at X" in capsys.readouterr().out


def test_cli_prints_a_rejected_credential_instead_of_a_traceback(monkeypatch, capsys):
    """★ witan-code's handler has to list this too.

    The remote path can now raise `RemoteCredentialRejected` — on a write, or on
    a read whose refreshed retry is refused again. The `witan` CLI was updated
    and this one was not, so it would have escaped as the traceback the whole
    handler exists to prevent.
    """
    from types import SimpleNamespace

    from witan_code import cli as cli_module
    from witan_code.remote.proxy import RemoteCredentialRejected

    def _rejected():
        raise RemoteCredentialRejected("witan-code: it rejected the credential")

    monkeypatch.setattr(cli_module, "app", SimpleNamespace(meta=_rejected))
    with pytest.raises(SystemExit) as exit_code:
        cli_module.cli()
    assert exit_code.value.code == 1
    assert "rejected the credential" in capsys.readouterr().out
