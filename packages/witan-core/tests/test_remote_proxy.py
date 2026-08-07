"""RemoteMCPProxy generic dispatch/arg-mapping/policy hooks (witan_core.remote).

The end-to-end "dispatch a real tool call over an in-memory FastMCP server" test
lives in witan-council (it needs witan's server + tools). Here we exercise the
transport-agnostic mechanism in isolation: the keyword-only argument contract,
the repo resolver hook, the None-dropping, and the admin/unknown refusal
wording.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx2
import pytest

from witan_core.remote.proxy import (
    RemoteMCPProxy,
    RemoteToolUnavailable,
    RemoteUnreachable,
    _tool_input_schema,
    _transport_failure,
    console_elicitation_handler,
)


class _Proxy(RemoteMCPProxy):
    """A proxy whose policy hooks are set, with arg-map schema pre-seeded."""

    def __init__(self, *, repo=None, admin=frozenset(), session=None):
        super().__init__("http://unused/mcp", lambda: "tok")
        self._repo = repo
        self._admin = admin
        self._session = session
        # Pre-seed the tool schema so _map_args needs no network.
        #
        # ALPHABETICAL, not signature order — this mirrors what the DEPLOYED
        # tier sends. JSON Schema `properties` is an unordered map by
        # specification, and the two servers this code talks to genuinely
        # disagree: an in-memory FastMCP publishes signature order, the
        # deployment publishes alphabetical. Both conform. A fixture in
        # signature order would therefore model only the friendlier of the two,
        # which is how order-based binding passed its tests while misbinding 29
        # of 41 tools in production.
        self._param_names = {
            "task_get": ["slug"],
            "task_ready": ["repo"],
            "task_create": ["description", "repo", "title"],
            "memory_store": ["content", "kind", "repo", "session_slug", "title"],
            # A real zero-parameter tool — witan-code's `code_indexed_repos`
            # declares no properties at all.
            "code_indexed_repos": [],
        }

    def _is_admin_tool(self, name):
        return name in self._admin

    def _admin_error(self, name):
        return f"{name} is admin-only here"

    def _unknown_tool_error(self, name):
        return f"no such tool: {name}"

    def _resolve_repo(self):
        return self._repo

    def _resolve_session_slug(self):
        return self._session


# ── cross-version tool-schema access ──────────────────────────────────────
# `fastmcp>=3.4.2,<5` spans the MCP SDK v1→v2 rename of `Tool.inputSchema` to
# `input_schema`. Any single CI run installs exactly one of those, so these
# stand in for the shapes rather than the real Tool class — otherwise the
# version CI doesn't happen to resolve goes permanently untested.

SCHEMA = {"properties": {"slug": {"type": "string"}}}


class _LegacyTool:
    """fastmcp 3.4.x: camelCase only."""

    inputSchema = SCHEMA


class _ModernTool:
    """fastmcp 4.x: snake_case, with the old name kept as a warning shim."""

    input_schema = SCHEMA

    @property
    def inputSchema(self):
        raise AssertionError(
            "read the deprecated camelCase field on a v2 tool (it warns)"
        )


def test_input_schema_read_from_legacy_camel_case_field():
    assert _tool_input_schema(_LegacyTool()) == SCHEMA


def test_input_schema_prefers_the_modern_field_and_never_reads_the_alias():
    # _ModernTool raises if the deprecated camelCase alias is touched, so this
    # fails loudly if the fallback order is ever inverted.
    assert _tool_input_schema(_ModernTool()) == SCHEMA


def test_param_name_extraction_works_on_both_shapes():
    # The expression _invoke actually builds _param_names with.
    for tool in (_LegacyTool(), _ModernTool()):
        names = list(_tool_input_schema(tool).get("properties", {}).keys())
        assert names == ["slug"]


def test_positional_arg_is_refused():
    # MCP carries arguments by name; the protocol defines no parameter order,
    # so there is nothing to map a positional onto. Refusing is the fix for the
    # misbinding described in the module docstring — a caller that still passes
    # positionally must be found here rather than silently writing bad data.
    p = _Proxy()
    with pytest.raises(RemoteToolUnavailable, match="by keyword"):
        p._map_args("task_get", ("s-1",), {})


def test_keyword_args_pass_through():
    p = _Proxy()
    assert p._map_args("task_get", (), {"slug": "s-1"}) == {"slug": "s-1"}


def test_refusal_names_the_accepted_parameters():
    # The message has to be actionable: the caller needs to know what to name
    # the argument it just passed positionally.
    p = _Proxy()
    with pytest.raises(RemoteToolUnavailable, match="content, kind, repo"):
        p._map_args("memory_store", ("lesson",), {})


def test_refusal_on_a_zero_parameter_tool_says_so():
    # `code_indexed_repos` declares no properties, so listing accepted names
    # would render as "Accepted names: ." — say what is actually wrong instead.
    p = _Proxy()
    with pytest.raises(RemoteToolUnavailable, match="accepts no arguments"):
        p._map_args("code_indexed_repos", ("oops",), {})


def test_zero_parameter_tool_still_works_with_no_args():
    p = _Proxy()
    assert p._map_args("code_indexed_repos", (), {}) == {}


def test_fixture_order_differs_from_signature_order():
    # Guards the fixture above, not the code. `memory_store`'s signature starts
    # with `kind`; this seeds what a deployed server actually sends, which is
    # alphabetical. If someone "tidies" the fixture back into signature order,
    # these tests would stop resembling production — the exact blind spot that
    # let order-based binding look correct for so long.
    p = _Proxy()
    props = p._param_names["memory_store"]
    assert props == sorted(props)
    assert props[0] != "kind"


def test_repo_none_is_resolved_via_hook():
    p = _Proxy(repo="https://github.com/test/repo")
    assert p._map_args("task_ready", (), {})["repo"] == "https://github.com/test/repo"


def test_repo_none_dropped_when_resolver_returns_none():
    p = _Proxy(repo=None)
    assert "repo" not in p._map_args("task_ready", (), {})


def test_repo_empty_string_sentinel_is_preserved():
    p = _Proxy(repo="https://other/repo")
    # "" (all repos) must NOT be replaced by detection.
    assert p._map_args("task_ready", (), {"repo": ""})["repo"] == ""


def test_session_slug_is_injected_when_omitted():
    # The protocol carries no session state, so provenance depends on the client
    # sending the handle it holds.
    p = _Proxy(session="ws-abc")
    out = p._map_args(
        "memory_store", (), {"kind": "lesson", "title": "t", "content": "c"}
    )
    assert out["session_slug"] == "ws-abc"


def test_explicit_session_slug_is_not_overwritten():
    p = _Proxy(session="ws-ambient")
    out = p._map_args(
        "memory_store",
        (),
        {"kind": "lesson", "title": "t", "content": "c", "session_slug": "ws-mine"},
    )
    assert out["session_slug"] == "ws-mine"


def test_session_slug_dropped_when_no_active_session():
    p = _Proxy(session=None)
    assert "session_slug" not in p._map_args(
        "memory_store", (), {"kind": "lesson", "title": "t", "content": "c"}
    )


def test_session_slug_not_added_to_tools_without_the_param():
    p = _Proxy(session="ws-abc")
    assert "session_slug" not in p._map_args("task_ready", (), {"repo": ""})


def test_none_optionals_are_dropped():
    p = _Proxy()
    out = p._map_args(
        "task_create", (), {"title": "t", "description": None, "repo": "r"}
    )
    assert out == {"title": "t", "repo": "r"}


def test_unknown_tool_raises_with_hook_message():
    p = _Proxy()
    with pytest.raises(RemoteToolUnavailable, match="no such tool: nope"):
        p._map_args("nope", (), {})


def test_extra_positionals_refused_not_indexerror():
    # More positionals than the tool has params must still be the keyword
    # refusal, never an IndexError from indexing past the end of the name list.
    p = _Proxy()
    with pytest.raises(RemoteToolUnavailable, match="by keyword"):
        p._map_args("task_get", ("a", "b"), {})


def test_admin_tool_is_refused_by_getattr():
    p = _Proxy(admin=frozenset({"merge_store"}))
    with pytest.raises(RemoteToolUnavailable, match="admin-only"):
        p.merge_store()


def test_non_admin_attr_returns_callable():
    p = _Proxy()
    # A non-admin tool name yields a dispatch callable (invocation would hit the
    # network, so we only assert it's callable here).
    assert callable(p.task_ready)


def test_dunder_attributes_are_not_intercepted():
    p = _Proxy()
    with pytest.raises(AttributeError):
        p.__wrapped__


# ── an unreachable deployment ─────────────────────────────────────────────
# With a remote configured and the endpoint down, every command used to exit
# with a raw Python traceback: `_invoke` had no handling and `main()` caught
# only the two auth/tool refusals. The classification mirrors witan-code's
# ClusterUnreachable — "I could not ask the service" is not "the service said
# no", and hard-failing (rather than falling back to the local store) is the
# deliberate behaviour being explained, not changed.

ENDPOINT = "https://witan.example.org/mcp"


class _ScriptedProxy(RemoteMCPProxy):
    """A proxy whose client fails at a chosen point, with a chosen exception."""

    def __init__(self, exc, *, at):
        super().__init__(ENDPOINT, lambda: "tok")
        self._exc = exc
        self._at = at
        # Seeded so a call reaches call_tool without a tools/list round trip.
        self._param_names = {"task_get": ["slug"]}

    def _unreachable_hint(self):
        return "HINT."

    def _new_client(self, token):
        exc, at = self._exc, self._at

        class _Client:
            async def __aenter__(self):
                if at == "connect":
                    raise exc
                return self

            async def __aexit__(self, *_):
                if at == "teardown":
                    raise exc
                return False

            async def call_tool(self, name, arguments):
                if at == "call":
                    raise exc
                return SimpleNamespace(data={"slug": arguments["slug"]})

        return _Client()


def test_a_failed_connection_is_classified_not_raised_raw():
    # fastmcp reports every connect-time failure — DNS, TLS, refused, a 5xx
    # from an ingress, a token the server rejects — as this bare RuntimeError.
    proxy = _ScriptedProxy(
        RuntimeError("Client failed to connect: All connection attempts failed"),
        at="connect",
    )
    with pytest.raises(RemoteUnreachable) as caught:
        proxy.task_get(slug="x")
    message = str(caught.value)
    assert ENDPOINT in message
    assert "All connection attempts failed" in message
    assert "HINT." in message


def test_the_underlying_error_is_kept_as_the_cause():
    original = RuntimeError("Client failed to connect: nope")
    proxy = _ScriptedProxy(original, at="connect")
    with pytest.raises(RemoteUnreachable) as caught:
        proxy.task_get(slug="x")
    assert caught.value.__cause__ is original


def test_a_drop_mid_call_is_unreachable_through_the_exception_group():
    # anyio's task groups re-raise a failed request inside an ExceptionGroup, so
    # the httpx2 error is never the exception the caller sees. Matching only the
    # outermost type would leave "the pod restarted during my write" a traceback.
    dropped = httpx2.ReadError("connection reset by peer")
    proxy = _ScriptedProxy(ExceptionGroup("unhandled", [dropped]), at="call")
    with pytest.raises(RemoteUnreachable, match="connection reset by peer"):
        proxy.task_get(slug="x")


def test_a_drop_noticed_only_while_closing_is_still_unreachable():
    # The call succeeded; the failure surfaces from `AsyncExitStack.__aexit__`,
    # which re-raises what fastmcp's anyio background tasks failed with. That
    # is outside the call itself, so a guard sitting *inside* the stack would
    # let exactly the traceback this change removes escape from teardown.
    dropped = httpx2.ReadError("server disconnected")
    proxy = _ScriptedProxy(ExceptionGroup("unhandled", [dropped]), at="teardown")
    with pytest.raises(RemoteUnreachable, match="server disconnected"):
        proxy.task_get(slug="x")


def test_a_non_transport_cleanup_error_still_propagates_as_itself():
    # Only transport faults are reclassified. A cleanup bug is a bug, and
    # relabelling it "the deployment could not be reached" would send whoever
    # hits it to check DNS for a defect in this process.
    proxy = _ScriptedProxy(ValueError("bad state in close()"), at="teardown")
    with pytest.raises(ValueError, match="bad state in close"):
        proxy.task_get(slug="x")


def test_a_server_side_tool_error_is_not_reclassified():
    # The deployment WAS reached and answered. Relabelling that as unreachable
    # would send the reader to check DNS for a task that simply does not exist.
    from fastmcp.exceptions import ToolError

    proxy = _ScriptedProxy(ToolError("no task with slug 'x'"), at="call")
    with pytest.raises(ToolError):
        proxy.task_get(slug="x")


def test_a_keyword_refusal_still_beats_the_unreachable_guard():
    # _map_args runs inside the guarded block; its refusal is a caller bug, not
    # a transport fault, and must keep its own actionable wording.
    proxy = _ScriptedProxy(None, at="never")
    with pytest.raises(RemoteToolUnavailable, match="by keyword"):
        proxy.task_get("x")


def test_a_genuinely_closed_port_raises_the_same_way():
    # The hermetic tests above script fastmcp's failure shape; this one proves
    # the shape is real, through the actual client stack against a port nothing
    # is listening on.
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    proxy = RemoteMCPProxy(f"http://127.0.0.1:{port}/mcp", lambda: "tok")
    with pytest.raises(RemoteUnreachable, match=f"127.0.0.1:{port}"):
        proxy.task_get(slug="x")


def test_transport_failure_terminates_on_a_cause_cycle():
    # __context__ chains can be cyclic; the walk must answer, not recurse
    # forever, and a cycle of non-transport errors is still "not a transport
    # fault".
    first, second = RuntimeError("a"), RuntimeError("b")
    first.__context__ = second
    second.__context__ = first
    assert _transport_failure(first) is None


# ── answering a deployed server's elicitation prompt ──────────────────────
# A person is at the terminal, so the CLI can put the ask to them instead of
# degrading to the tool's default the way an agent-hosted client does.


class _Tty:
    def isatty(self):
        return True


def _ask(monkeypatch, answer, *, boolean, tty=True):
    """Run the console handler with ``answer`` typed at the prompt."""
    monkeypatch.setattr("sys.stdin", _Tty() if tty else None)
    if isinstance(answer, type) and issubclass(answer, BaseException):

        def _input(_prompt):
            raise answer

    else:

        def _input(_prompt):
            return answer

    monkeypatch.setattr("builtins.input", _input)
    field = {"type": "boolean" if boolean else "string"}
    params = SimpleNamespace(
        requested_schema={"type": "object", "properties": {"value": field}}
    )
    return asyncio.run(console_elicitation_handler("Proceed?", None, params, None))


def test_typed_answers_are_accepted(monkeypatch):
    assert _ask(monkeypatch, "y", boolean=True).content == {"value": True}
    assert _ask(monkeypatch, "YES", boolean=True).content == {"value": True}
    # anything that isn't a yes is a no — not a decline, which would instead
    # hand the tool its unsupported-client default.
    assert _ask(monkeypatch, "n", boolean=True).content == {"value": False}
    assert _ask(monkeypatch, "  a value  ", boolean=False).content == {
        "value": "a value"
    }


@pytest.mark.parametrize("answer", ["", "   ", EOFError, KeyboardInterrupt])
def test_no_answer_declines(monkeypatch, answer):
    # Blank, Ctrl-D, and Ctrl-C all mean "don't ask me", which every witan call
    # site maps onto the same default a client that can't elicit would get.
    assert _ask(monkeypatch, answer, boolean=True).action == "decline"


def test_without_a_terminal_declines_without_reading_stdin(monkeypatch):
    def _fail(_prompt):
        raise AssertionError("must not read stdin when there is no terminal")

    monkeypatch.setattr("sys.stdin", None)
    monkeypatch.setattr("builtins.input", _fail)
    params = SimpleNamespace(
        requested_schema={"properties": {"value": {"type": "boolean"}}}
    )
    result = asyncio.run(console_elicitation_handler("Proceed?", None, params, None))
    assert result.action == "decline"


def test_client_is_built_with_the_handler(monkeypatch):
    # Advertising the capability is what makes a deployed tool ask at all, so a
    # proxy that dropped the handler would silently get the defaults instead.
    # Asserted on the constructor kwarg rather than a Client attribute, which
    # differs across the fastmcp 3.4.x/4.x range this package supports.
    built = {}
    monkeypatch.setattr(
        "witan_core.remote.proxy.Client",
        lambda transport, **kwargs: built.update(kwargs) or object(),
    )
    _Proxy()._new_client("tok")
    assert built["elicitation_handler"] is console_elicitation_handler


# ── honoring the server's cache directive ─────────────────────────────────
# The tool list used to be held for the whole process lifetime — a guess. MCP
# 2026-07-28 has the server state how long its list results stay fresh, so the
# proxy holds it for exactly that long instead.


class _CountingProxy(RemoteMCPProxy):
    """A proxy wired to an in-memory server, counting its tools/list calls."""

    def __init__(self, server, mode="auto"):
        super().__init__("http://unused/mcp", lambda: "tok")
        self._server = server
        self._mode = mode
        self.lists = 0

    def _new_client(self, token):
        from fastmcp import Client

        client = Client(self._server, mode=self._mode)
        original = client.list_tools_mcp

        async def _counted(*args, **kwargs):
            self.lists += 1
            return await original(*args, **kwargs)

        client.list_tools_mcp = _counted
        return client


def _echo_server(**cache_kwargs):
    from fastmcp import FastMCP

    server = FastMCP("cache-test", **cache_kwargs)

    @server.tool
    def echo(value: str) -> str:
        """Echo the value back."""
        return value

    return server


def test_declared_ttl_bounds_how_long_the_list_is_held(monkeypatch):
    from witan_core import caching

    proxy = _CountingProxy(_echo_server(**caching.hint_kwargs(ttl_seconds=300)))
    assert proxy.echo(value="a") == "a"
    assert proxy.lists == 1

    # Inside the window the cached list is reused...
    assert proxy.echo(value="b") == "b"
    assert proxy.lists == 1

    # ...and past it the proxy re-lists rather than serving a stale surface.
    expiry = proxy._param_names_expiry
    monkeypatch.setattr("time.monotonic", lambda: expiry + 1)
    assert proxy.echo(value="c") == "c"
    assert proxy.lists == 2


def test_a_zero_ttl_is_an_instruction_not_a_missing_value():
    # A 2026-07-28 server that sets no hint still sends ttlMs=0, which says
    # "do not cache this" — so the proxy re-reads rather than treating the
    # absence of a *configured* hint as permission to hold the list forever.
    proxy = _CountingProxy(_echo_server())
    assert proxy.echo(value="a") == "a"
    assert proxy.echo(value="b") == "b"
    assert proxy.lists == 2


def test_connection_without_the_field_keeps_the_process_lifetime_cache():
    # A handshake-era peer carries no ttlMs at all, so there is nothing to
    # honor — hold the list as the proxy always did rather than adding a
    # round trip to every call.
    import math

    proxy = _CountingProxy(_echo_server(), mode="legacy")
    assert proxy.echo(value="a") == "a"
    assert proxy._param_names_expiry == math.inf
    assert proxy.echo(value="b") == "b"
    assert proxy.lists == 1


class _PagedProxy(RemoteMCPProxy):
    """A proxy whose tools/list is a canned sequence of pages."""

    def __init__(self, pages):
        super().__init__("http://unused/mcp", lambda: "tok")
        self._pages = pages

    async def refresh(self):
        proxy = self

        class _Client:
            async def list_tools_mcp(self, cursor=None):
                index = 0 if cursor is None else int(cursor)
                return proxy._pages[index]

        await self._refresh_param_names(_Client())


def _page(ttl_ms, next_cursor, *, declared=True):
    """One tools/list page, with `ttl_ms` either declared on the wire or not."""
    fields = {"ttl_ms"} if declared else set()
    return SimpleNamespace(
        tools=[
            SimpleNamespace(input_schema={"properties": {"value": {}}}, name="echo")
        ],
        ttl_ms=ttl_ms,
        next_cursor=next_cursor,
        model_fields_set=fields,
    )


def test_shortest_declared_ttl_across_pages_wins(monkeypatch):
    monkeypatch.setattr("time.monotonic", lambda: 1000.0)
    # Shorter TTL first, so reading only the last page gives the wrong answer.
    proxy = _PagedProxy([_page(60_000, "1"), _page(300_000, None)])
    asyncio.run(proxy.refresh())
    assert proxy._param_names_expiry == 1000.0 + 60.0


def test_a_later_undeclared_page_cannot_upgrade_a_ttl_to_forever(monkeypatch):
    # Reading only the last page would turn "cache me for 5 minutes" into
    # "cache me forever" — the one direction that is unsafe.
    monkeypatch.setattr("time.monotonic", lambda: 1000.0)
    proxy = _PagedProxy([_page(300_000, "1"), _page(0, None, declared=False)])
    asyncio.run(proxy.refresh())
    assert proxy._param_names_expiry == 1000.0 + 300.0
