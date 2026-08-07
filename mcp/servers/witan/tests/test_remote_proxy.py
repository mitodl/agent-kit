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
from witan.remote.proxy import (
    RemoteServerProxy,
    RemoteToolUnavailable,
    RemoteUnreachable,
)

from .conftest import requires_omnigraph

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


def test_keyword_args_reach_the_server(proxy):
    created = proxy.task_create(title="probe", description="d", repo=REPO)
    slug = created["slug"]
    fetched = proxy.task_get(slug=slug)
    assert fetched["slug"] == slug
    assert fetched["title"] == "probe"


def test_positional_arg_is_refused(proxy):
    # MCP is keyword-only on the wire. The proxy used to map positionals onto
    # the input schema's property order; that silently misbound arguments (see
    # witan_core.remote.proxy's module docstring), so it now refuses.
    with pytest.raises(RemoteToolUnavailable, match="by keyword"):
        proxy.task_get("tk-anything")


def test_schema_property_order_is_not_a_contract(proxy):
    """Why the old positional mapping survived its own tests for so long.

    This in-memory server publishes ``memory_store``'s properties in SIGNATURE
    order (``kind`` first), so positional binding looked correct here. The
    deployed tier publishes the same tool ALPHABETICALLY (``category`` first) —
    measured 2026-08-06 against witan.ci.ol.mit.edu, where 29 of 41 tools bound
    their first positional to the wrong parameter.

    Both are legal: JSON Schema ``properties`` is an unordered map, so no order
    is promised and two conforming servers may disagree. That is precisely why
    the proxy must not read order at all — and why no local test could have
    caught the bug by asserting an order. The real guard is
    ``test_positional_arg_is_refused``; this test exists to stop anyone
    "restoring" positional support after observing that it works in-process.
    """
    proxy.task_ready(repo="")  # force a tools/list so _param_names is populated
    props = proxy._param_names["memory_store"]
    assert "kind" in props and "category" in props
    # Do NOT assert an order here — asserting either one would bless a
    # deployment-specific accident as a contract.
    with pytest.raises(RemoteToolUnavailable, match="by keyword"):
        proxy.memory_store("lesson", "t", "c")


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


def test_session_handle_is_threaded_from_the_client(
    proxy, server, tmp_path, monkeypatch
):
    """A memory stored over the proxy carries SessionProduced provenance.

    The deployed server cannot resolve the session itself — no protocol session
    state, no shared filesystem — so the proxy sends the handle the client parked.
    """
    import witan.server as srv

    monkeypatch.setattr(srv, "_active_session_slug", lambda: None)
    monkeypatch.setattr(
        "witan.remote.proxy.repo_module.detect", lambda override=None: REPO
    )
    monkeypatch.setattr("witan.session_state.session_state_dir", lambda: tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-remote-1")

    proj = proxy.workflow_project_create(title="remote", description="d")
    handle = proxy.workflow_session_start(
        project_slug=proj["slug"], session_id="sess-remote-1", phase="implementation"
    )
    # A deployed server never writes the handle file; the CLI does.
    from witan import session_state

    session_state.write_handle("sess-remote-1", dict(handle))

    mem = proxy.memory_store(
        kind="lesson", title="remote", content="c", severity="info"
    )

    assert mem["session_linked"] is True
    grouped = proxy.workflow_project_memories(
        project_slug=proj["slug"], group_by_session=True
    )
    assert mem["slug"] in {
        m["slug"] for m in grouped["by_session"][handle["session_slug"]]
    }


def test_no_parked_handle_means_no_provenance(proxy, tmp_path, monkeypatch):
    monkeypatch.setattr("witan.session_state.session_state_dir", lambda: tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-absent")

    assert proxy._resolve_session_slug() is None
    mem = proxy.memory_store(kind="pattern", title="unlinked", content="c", repo=REPO)
    assert mem["session_linked"] is False


def test_admin_only_functions_are_refused_without_network(proxy):
    for name in ("migrate_topics", "apply_schema"):
        with pytest.raises(RemoteToolUnavailable, match="in-cluster"):
            getattr(proxy, name)()


def test_merge_store_is_no_longer_admin_only(proxy):
    """`merge_store` has a per-actor form now, so it must NOT be refused.

    It was admin-only for as long as the only way to merge was shelling out to
    omnigraph against the data tier, which has no per-user identity. ADR-0007
    D5 gives it one — the proxy exports locally and ships rows through
    ``store_merge`` — so the refusal would now block the *supported* path.
    """
    from witan.remote.proxy import _ADMIN_ONLY

    assert "merge_store" not in _ADMIN_ONLY
    assert not proxy._is_admin_tool("merge_store")


def test_remote_merge_refuses_an_explicit_target(proxy, tmp_path):
    """Over a deployment the target is the deployment's own graph.

    A client never names a store address (ADR-0005 c) — the server resolves it
    from its own config. Silently ignoring `--target` would let someone believe
    they had merged into the store they named.
    """
    source = tmp_path / "handover.jsonl"
    source.write_text("")

    with pytest.raises(RemoteToolUnavailable, match="not accepted"):
        proxy.merge_store(str(source), target="/some/other/store.omni")


@requires_omnigraph
def test_remote_merge_lands_rows_through_the_mcp_tier(proxy, server, tmp_path):
    """The whole ADR-0007 D5 path, end to end: local store -> MCP -> deployed graph.

    Nothing here shells out to omnigraph against the *target*: the proxy exports
    only the source, and `store_merge` reconciles against the graph it already
    holds a client on. That is what makes the write authorized as the caller
    rather than as a service account.
    """
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import server as srv

    from .test_migrate import _init_store, _insert_memory

    source = graph_mod.OmnigraphClient(
        _init_store(tmp_path / "mine.omni"), cfg_mod.load().queries_dir
    )
    _insert_memory(
        source,
        slug="mem-through-the-tier-a1b2c3",
        content="shipped over MCP",
        updated_at="2026-01-01T00:00:00Z",
    )

    result = proxy.merge_store(source.graph_uri)

    assert (result["added"], result["updated"], result["kept_target"]) == (1, 0, 0)
    assert result["rows_loaded"] == 1
    rows = srv.client.read(
        "read.gq", "get_memory", {"slug": "mem-through-the-tier-a1b2c3"}
    )
    assert rows and rows[0]["content"] == "shipped over MCP"

    # Idempotent across the batched path: the row now loses reconciliation to
    # its own already-applied copy, so a re-run writes nothing.
    again = proxy.merge_store(source.graph_uri)
    assert (again["added"], again["updated"], again["kept_target"]) == (0, 0, 1)
    assert again["rows_loaded"] == 0


@requires_omnigraph
def test_remote_merge_dry_run_writes_nothing(proxy, server, tmp_path):
    """`--dry-run` has to work over this transport too — it is the review step
    the cutover runbook makes mandatory before a non-reversible merge."""
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import server as srv

    from .test_migrate import _init_store, _insert_memory

    source = graph_mod.OmnigraphClient(
        _init_store(tmp_path / "mine.omni"), cfg_mod.load().queries_dir
    )
    _insert_memory(
        source,
        slug="mem-dry-run-only-d4e5f6",
        content="should not land",
        updated_at="2026-01-01T00:00:00Z",
    )

    result = proxy.merge_store(source.graph_uri, dry_run=True)

    assert result["dry_run"] is True
    assert result["added"] == 1
    assert result["rows_loaded"] == 0
    assert [d["slug"] for d in result["decisions"]] == ["mem-dry-run-only-d4e5f6"]
    assert not srv.client.read(
        "read.gq", "get_memory", {"slug": "mem-dry-run-only-d4e5f6"}
    )


def test_remote_merge_from_a_remote_export_says_to_download_it(proxy):
    """Same rule as the in-process path, which this client half had not mirrored.

    A remote `.jsonl` fell through to the local-file branch and came back as
    "no such export file" — pointing at the path when the problem is that witan
    fetches no remote exports.
    """
    for uri in (
        "s3://ol-data-witan-ci/alice.jsonl",
        "https://example.invalid/alice.jsonl",
    ):
        with pytest.raises(RemoteToolUnavailable, match="does not fetch remote ones"):
            proxy.merge_store(uri)


def test_admin_only_functions_are_not_registered_as_tools(server):
    """The server-side half of the admin refusal, and the load-bearing one.

    ``RemoteServerProxy._is_admin_tool`` runs in the *client*, so it is advisory
    — a stock MCP client with a valid JWT ignores it entirely. What actually
    keeps ``apply_schema``/``migrate_*`` unreachable is that they are
    deliberately plain module functions, never ``@mcp.tool``. Assert that
    invariant here so a future decorator can't silently expose an admin op with
    no per-user identity to the whole deployment.

    ``store_merge`` is the deliberate counter-example: it *is* a tool, because
    it resolves the caller's actor per request and writes as them.
    """
    import witan.server as srv
    from witan.remote.proxy import _ADMIN_ONLY

    async def _list() -> set[str]:
        async with Client(srv.mcp) as client:
            return {t.name for t in await client.list_tools()}

    exposed = asyncio.run(_list())

    assert exposed, "expected the in-memory server to expose some tools"
    assert not (_ADMIN_ONLY & exposed)
    assert "store_merge" in exposed


def test_memory_repair_tools_are_not_admin_only(server):
    """``memory_update``/``memory_delete`` are per-user, author-scoped ops, not
    identity-less admin ones — they must stay usable over the remote CLI like
    the rest of the memory surface."""
    import witan.server as srv
    from witan.remote.proxy import _ADMIN_ONLY

    async def _list() -> set[str]:
        async with Client(srv.mcp) as client:
            return {t.name for t in await client.list_tools()}

    exposed = asyncio.run(_list())

    assert {"memory_update", "memory_delete"} <= exposed
    assert not (_ADMIN_ONLY & {"memory_update", "memory_delete"})


def test_unknown_tool_is_refused(proxy):
    with pytest.raises(RemoteToolUnavailable):
        proxy.definitely_not_a_tool(repo="")


# ── an unreachable deployment ─────────────────────────────────────────────
# The generic classification is pinned in witan-core; what witan owns is the
# wording — which endpoint, why there is no local fallback, and which of the two
# settings that could have routed the caller here to unset.


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
    # The behaviour is deliberate (docs/deployed-witan-onboarding.md) and the
    # reason has to travel with the error: a user who assumes the command
    # quietly went local finds out at merge time, which is far too late.
    message = _dead()
    assert "does not fall back" in message
    assert "split your memory" in message


@pytest.mark.parametrize(
    "source",
    [
        "`WITAN_REMOTE_URL`",
        "`remote_url` on target [qa]",
        "`remote_url` in config.toml",
    ],
)
def test_unreachable_message_names_the_setting_that_supplied_the_url(source):
    assert source in _dead(url_source=source)


def test_unreachable_message_does_not_infer_the_setting_from_the_target():
    # The trap: a matched target does NOT mean the target supplied the URL.
    # `WITAN_REMOTE_URL` overrides a matched target's `remote_url` while
    # leaving `target_name` set (test_config.py's
    # `test_load_remote_config_env_overrides_target`), so inferring from
    # `target_name` would tell this user to unset a key that is present but
    # overridden — and they would still be routed at the dead endpoint after
    # doing it. `url_source` is the resolver's own record of which won.
    message = _dead(target_name="qa", url_source="`WITAN_REMOTE_URL`")
    assert "`WITAN_REMOTE_URL`" in message
    assert "target [qa]" not in message


def test_main_prints_an_unreachable_remote_instead_of_a_traceback(monkeypatch, capsys):
    # The entrypoint guard: `main()` caught only the auth and unknown-tool
    # refusals, so this one escaped as a raw Python traceback.
    from types import SimpleNamespace

    from witan import cli as cli_module

    def _down():
        raise RemoteUnreachable("witan is down at X")

    monkeypatch.setattr(cli_module, "app", SimpleNamespace(meta=_down))
    with pytest.raises(SystemExit) as exit_code:
        cli_module.main()
    assert exit_code.value.code == 1
    assert "witan is down at X" in capsys.readouterr().out


def test_main_does_not_let_rich_swallow_a_bracketed_target_name(monkeypatch, capsys):
    """A target block is written `[qa]`, which rich parses as a style tag.

    Printing through `f"[red]{exc}[/red]"` therefore ate the one part of the
    sentence that says which setting to unset — "unset `remote_url` on target
    [qa]" reached the user as "…on target". Caught only by running the real
    command, since asserting on `str(exc)` never goes through the console.
    """
    from types import SimpleNamespace

    from witan import cli as cli_module

    def _down():
        raise RemoteUnreachable("unset `remote_url` on target [qa] to work locally")

    monkeypatch.setattr(cli_module, "app", SimpleNamespace(meta=_down))
    with pytest.raises(SystemExit):
        cli_module.main()
    # Rendered output wraps, so match the fragment that markup would remove.
    assert "[qa]" in capsys.readouterr().out


def test_srv_surfaces_misconfigured_remote_as_clean_exit(monkeypatch):
    # WITAN_REMOTE_URL without WITAN_OIDC_ISSUER makes load_remote_config raise
    # ValueError; _srv() must turn that into a clean SystemExit, not a traceback.
    from witan.cli import _common

    monkeypatch.setenv("WITAN_REMOTE_URL", "https://witan.example.org/mcp")
    monkeypatch.delenv("WITAN_OIDC_ISSUER", raising=False)
    monkeypatch.setattr(_common, "_server", None)
    with pytest.raises(SystemExit):
        _common._srv()
