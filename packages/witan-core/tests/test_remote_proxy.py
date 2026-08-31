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
    RemoteCredentialRejected,
    RemoteMCPProxy,
    RemotePayloadTooLarge,
    RemoteToolFailed,
    RemoteToolUnavailable,
    RemoteUnreachable,
    RemoteWriteIndeterminate,
    _tool_input_schema,
    _transport_failure,
    console_elicitation_handler,
    payload_too_large,
    tool_failure,
)


class _Proxy(RemoteMCPProxy):
    """A proxy whose policy hooks are set, with arg-map schema pre-seeded."""

    def __init__(
        self,
        *,
        repo=None,
        admin=frozenset(),
        session=None,
        no_detect=frozenset(),
        branch=None,
        branch_tools=frozenset(),
    ):
        super().__init__("http://unused/mcp", lambda: "tok")
        self._repo = repo
        self._admin = admin
        self._session = session
        self._no_detect = no_detect
        self._branch = branch
        self._branch_tools = branch_tools
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
            "task_claim": ["branch", "repo", "slug"],
            # Declares `branch` with the OTHER meaning: a branch the caller is
            # asking about, not the one they are standing on.
            "code_indexed_branches": ["branch"],
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

    def _repo_means_detect(self, name):
        return name not in self._no_detect

    def _resolve_session_slug(self):
        return self._session

    def _resolve_branch(self):
        return self._branch

    def _branch_means_checkout(self, name):
        return name in self._branch_tools


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


def test_repo_not_injected_where_the_hook_says_it_is_an_update_field():
    """`repo=None` means "leave it" on an update tool, not "detect".

    Injecting there sends an explicit repo the caller never asked for, and the
    server applies it — rewriting the stored value to wherever the caller
    happened to be. The binding names those tools; the mechanism is here.
    """
    p = _Proxy(repo="https://github.com/test/repo", no_detect={"task_create"})
    assert "repo" not in p._map_args("task_create", (), {"title": "t"})
    # Same proxy, unlisted tool: detection still applies.
    assert p._map_args("task_ready", (), {})["repo"] == "https://github.com/test/repo"


def test_explicit_repo_is_sent_even_where_detection_is_off():
    # Opting out of *detection* must not drop a repo the caller passed on
    # purpose — that is how an update tool corrects a wrong repo.
    p = _Proxy(repo="https://github.com/test/repo", no_detect={"task_create"})
    args = p._map_args("task_create", (), {"title": "t", "repo": "https://x/y"})
    assert args["repo"] == "https://x/y"


def test_branch_is_injected_only_where_the_hook_opts_in():
    """`branch` cannot default the way `repo` does — the name carries two
    meanings across the surface, and only one of them is "the branch I am on".

    On a code-graph read it names a view inside the store, so injecting the
    caller's checked-out branch would silently re-point the read. Hence opt-in,
    and hence the default below is "not injected".
    """
    p = _Proxy(branch="feature/x", branch_tools={"task_claim"})
    assert p._map_args("task_claim", (), {"slug": "tk-1"})["branch"] == "feature/x"
    assert "branch" not in p._map_args("code_indexed_branches", (), {})


def test_branch_not_injected_when_no_tool_opts_in():
    # The base-class default: a binding that never classifies anything gets no
    # injection at all, which is the safe direction for this parameter.
    p = _Proxy(branch="feature/x")
    assert "branch" not in p._map_args("task_claim", (), {"slug": "tk-1"})


def test_branch_dropped_when_resolver_returns_none():
    # Detached HEAD, or outside a repo. Dropping leaves the tool's own default
    # rather than sending an explicit null.
    p = _Proxy(branch=None, branch_tools={"task_claim"})
    assert "branch" not in p._map_args("task_claim", (), {"slug": "tk-1"})


def test_explicit_branch_is_never_overwritten_by_detection():
    # The caller naming a branch outranks whatever they happen to have checked
    # out — same rule as `repo`.
    p = _Proxy(branch="feature/checked-out", branch_tools={"task_claim"})
    args = p._map_args("task_claim", (), {"slug": "tk-1", "branch": "feature/asked"})
    assert args["branch"] == "feature/asked"


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
        # `memory_store` is here as a NON-bulk write: a single call that can
        # still be refused for size, which is what pins the base message's
        # silence about batching.
        self._param_names = {"task_get": ["slug"], "memory_store": ["content"]}

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


def test_a_drop_carries_the_transport_error_as_its_cause():
    # One test per classified branch, all asserting the same rule, because the
    # branches drifted apart precisely when only one of them had a test that
    # said which exception belonged on __cause__.
    inner = httpx2.ReadError("connection reset by peer")
    group = ExceptionGroup("unhandled", [inner])
    proxy = _ScriptedProxy(group, at="call")
    with pytest.raises(RemoteUnreachable) as caught:
        proxy.task_get(slug="x")
    assert caught.value.__cause__ is inner
    assert caught.value.__context__ is group


def test_a_non_transport_cleanup_error_still_propagates_as_itself():
    # Only transport faults are reclassified. A cleanup bug is a bug, and
    # relabelling it "the deployment could not be reached" would send whoever
    # hits it to check DNS for a defect in this process.
    proxy = _ScriptedProxy(ValueError("bad state in close()"), at="teardown")
    with pytest.raises(ValueError, match="bad state in close"):
        proxy.task_get(slug="x")


def test_a_server_side_tool_error_is_not_reclassified_as_unreachable():
    # The deployment WAS reached and answered. Relabelling that as unreachable
    # would send the reader to check DNS for a task that simply does not exist.
    # It becomes RemoteToolFailed — its own type, carrying the server's own
    # words — and specifically NOT RemoteUnreachable.
    from fastmcp.exceptions import ToolError

    proxy = _ScriptedProxy(ToolError("no task with slug 'x'"), at="call")
    with pytest.raises(RemoteToolFailed, match="no task with slug 'x'") as caught:
        proxy.task_get(slug="x")
    assert not isinstance(caught.value, RemoteUnreachable)


def test_a_refused_call_is_catchable_as_the_runtime_error_the_cli_expects():
    # THE DEFECT THIS FIXES. `ToolError → FastMCPError → Exception` is not a
    # RuntimeError, so every `except RuntimeError` in the CLI missed it and a
    # server-side refusal on a remote target printed ~40 lines of asyncio
    # internals. In-process the same refusal IS a RuntimeError; this assertion
    # is the local/remote parity the proxy's drop-in claim depends on.
    from fastmcp.exceptions import ToolError

    proxy = _ScriptedProxy(ToolError("cedar: write denied on memory"), at="call")
    with pytest.raises(RuntimeError, match="cedar: write denied"):
        proxy.task_get(slug="x")


def test_the_wire_form_of_a_refusal_is_kept_as_the_cause():
    # "Must keep its own error" is about not losing what the server said, not
    # about the class that carries it. Anyone who needs the ToolError itself —
    # a caller inspecting fastmcp's own fields — still has it.
    from fastmcp.exceptions import ToolError

    raised = ToolError("no task with slug 'x'")
    proxy = _ScriptedProxy(raised, at="call")
    with pytest.raises(RemoteToolFailed) as caught:
        proxy.task_get(slug="x")
    assert caught.value.__cause__ is raised


def test_tool_failure_finds_nothing_in_an_unrelated_error():
    # Public because witan-code's store session holds its own connection and
    # does its own classification, the same reason payload_too_large is. A
    # helper that answered "yes" to anything would make every bug in that path
    # read as a server-side refusal.
    assert tool_failure(ValueError("bad state in close()")) is None
    assert tool_failure(httpx2.ReadError("connection reset by peer")) is None


def test_a_refusal_arriving_inside_an_exception_group_is_still_classified():
    # anyio re-raises through a task group, so the ToolError is not always the
    # outermost exception — the same reason _transport_failure walks the chain.
    from fastmcp.exceptions import ToolError

    inner = ToolError("no task with slug 'x'")
    group = ExceptionGroup("unhandled", [inner])
    proxy = _ScriptedProxy(group, at="call")
    with pytest.raises(RemoteToolFailed, match="no task with slug 'x'") as caught:
        proxy.task_get(slug="x")
    # And __cause__ is the ToolError, not the group it arrived inside. Chaining
    # the group would hand a caller the wrapper this class exists to unwrap,
    # making the "keeps the original ToolError" contract true only for the
    # ungrouped case. The group is still reachable as __context__.
    assert caught.value.__cause__ is inner
    assert caught.value.__context__ is group


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


# ── a body the deployment refuses for its size ────────────────────────────
# `witan migrate merge` against the CI deployment died with a raw
# `fastmcp.exceptions.ToolError: … 413: Request body too large`. The client
# bound (MCP_LOAD_MAX_BYTES) stops us producing those bodies; this is the other
# half — when one gets through anyway, it has to read as a sentence.


def _http_413() -> httpx2.HTTPStatusError:
    """What a DIRECT connection to a deployment raises on a 413."""
    request = httpx2.Request("POST", ENDPOINT)
    response = httpx2.Response(413, request=request, text="Request body too large")
    return httpx2.HTTPStatusError(
        "Client error '413 Request Entity Too Large'",
        request=request,
        response=response,
    )


# The exact text the vMCP relayed in the live failure, and the two other hops
# on this path that can refuse a body for its size.
_RELAYED = (
    'backend unavailable: tool call failed on backend witan: calling "tools/call": '
    'sending "tools/call": Request Entity Too Large: request failed with status '
    "413: Request body too large",
    "413 Payload Too Large: Failed to buffer the request body",
)


@pytest.mark.parametrize("text", _RELAYED)
def test_a_relayed_413_is_classified_not_raised_raw(text):
    # Through ToolHive's vMCP the HTTP exchange with us SUCCEEDS — the refusal
    # happened on its upstream call, so it arrives as a tool error whose only
    # trace of the 413 is the words. Nothing here has a status code to read.
    from fastmcp.exceptions import ToolError

    proxy = _ScriptedProxy(ToolError(text), at="call")
    with pytest.raises(RemotePayloadTooLarge) as caught:
        proxy.task_get(slug="x")
    assert "over its size cap" in str(caught.value)


def test_a_direct_413_is_too_large_and_NOT_unreachable():
    # ★ THE ORDERING TEST. httpx2.HTTPStatusError is an httpx2.HTTPError, so a
    # guard that asks `_transport_failure` first answers "the deployed service
    # could not be reached" about a deployment that is up and answering — and
    # sends the reader off to check DNS for a payload they need to shrink.
    proxy = _ScriptedProxy(ExceptionGroup("unhandled", [_http_413()]), at="call")
    with pytest.raises(RemotePayloadTooLarge) as caught:
        proxy.task_get(slug="x")
    assert not isinstance(caught.value, RemoteUnreachable)
    assert "could not be reached" not in str(caught.value)


def test_the_base_message_names_the_call_and_says_retrying_is_futile():
    from fastmcp.exceptions import ToolError

    proxy = _ScriptedProxy(ToolError("Request body too large"), at="call")
    with pytest.raises(RemotePayloadTooLarge) as caught:
        proxy.task_get(slug="x")
    message = str(caught.value)
    assert "`task_get`" in message  # which call
    assert ENDPOINT in message  # which deployment
    assert "has to get smaller" in message  # what to actually do


def test_the_base_message_claims_NOTHING_about_batching_or_partial_writes():
    """★ This message fires for EVERY tool call, not just byte-chunked writes.

    An earlier revision asserted here that "bulk writes are split into 2 MiB
    batches" and that "batches before this one were applied — the write stopped
    part-way". For a refused `memory_store`, a read, or a `--dry-run` merge,
    both are false — and the second is false in the direction that does harm:
    it tells someone whose graph was untouched that it is now half-mutated.

    Callers that genuinely are mid-batch add that context themselves, where the
    numbers are real (witan's `_merge_batch_refusal`, witan-code's
    `_load_refusal`).
    """
    from fastmcp.exceptions import ToolError

    proxy = _ScriptedProxy(ToolError("Request body too large"), at="call")
    with pytest.raises(RemotePayloadTooLarge) as caught:
        # A single-call write, not a bulk one — nothing was batched, and
        # nothing was partially applied.
        proxy.memory_store(content="x" * 10)
    message = str(caught.value).lower()
    for forbidden in ("batch", "part-way", "roll back", "mib", "applied"):
        assert forbidden not in message, f"base message must not mention {forbidden!r}"


def test_a_413_during_teardown_is_still_classified():
    # Same reason the unreachable guard wraps the exit stack rather than sitting
    # inside it: anyio re-raises a background failure while the client closes.
    proxy = _ScriptedProxy(ExceptionGroup("unhandled", [_http_413()]), at="teardown")
    with pytest.raises(RemotePayloadTooLarge):
        proxy.task_get(slug="x")


def test_an_ordinary_tool_error_is_not_mistaken_for_an_oversized_body():
    # Classification is by phrase, and a tool error relays the SERVER's text —
    # which can quote the caller's own data. Matching a bare "413" would turn a
    # memory that happens to mention one into a size refusal.
    from fastmcp.exceptions import ToolError

    proxy = _ScriptedProxy(
        ToolError("no memory with slug 'error-413-handling'"), at="call"
    )
    with pytest.raises(RemoteToolFailed) as caught:
        proxy.task_get(slug="x")
    assert not isinstance(caught.value, RemotePayloadTooLarge)


def test_a_relayed_413_is_still_a_size_refusal_though_it_arrives_as_a_tool_error():
    # ORDERING GUARD. ToolHive's vMCP relays an upstream 413 as a ToolError, so
    # the tool-refusal branch must be asked LAST. Asked first, it would file
    # every relayed 413 under "the tool refused" and lose the one reading that
    # tells the caller to send less rather than to fix the call.
    from fastmcp.exceptions import ToolError

    proxy = _ScriptedProxy(ToolError("upstream: 413 Request body too large"), at="call")
    with pytest.raises(RemotePayloadTooLarge):
        proxy.task_get(slug="x")


def test_an_ordinary_drop_is_still_unreachable_not_too_large():
    # The guard added above must not swallow the case it was inserted ahead of.
    dropped = httpx2.ReadError("connection reset by peer")
    proxy = _ScriptedProxy(ExceptionGroup("unhandled", [dropped]), at="call")
    with pytest.raises(RemoteUnreachable, match="connection reset by peer"):
        proxy.task_get(slug="x")


def test_payload_too_large_terminates_on_a_cause_cycle():
    # Shares `_chain` with `_transport_failure`, so it inherits the same cycle
    # guard — pinned here so a future rewrite of either cannot lose it.
    first, second = RuntimeError("a"), RuntimeError("b")
    first.__context__ = second
    second.__context__ = first
    assert payload_too_large(first) is None


def test_a_413_carries_the_underlying_error_as_its_cause():
    # The 413 itself on __cause__, not the group it arrived inside — the same
    # rule every branch of _reclassifying now follows. This used to assert the
    # group, which is the container the classifier had just walked _chain to
    # look past; a caller reading __cause__ had to redo that walk.
    inner = _http_413()
    group = ExceptionGroup("unhandled", [inner])
    proxy = _ScriptedProxy(group, at="call")
    with pytest.raises(RemotePayloadTooLarge) as caught:
        proxy.task_get(slug="x")
    assert caught.value.__cause__ is inner
    assert caught.value.__context__ is group


# ── a call the gateway cut off after dispatching it ───────────────────────
# OBSERVED LIVE 2026-08-12 against the CI deployment, counted from the rows
# afterwards: two 16-writer bursts, every 502 arriving at ~30.0s (ToolHive's
# hardcoded backend deadline). The first burst committed all 28 of the writes it
# 502'd on; the second committed 14 of 16. So the deadline cuts the RESPONSE,
# the backend usually finishes the write anyway, and the reply says nothing
# about which happened. These tests pin the one thing the client can honestly
# say about that, and stop it saying the two things that are false.


def _gateway_error(status: int) -> httpx2.HTTPStatusError:
    """What a direct connection raises when APISIX answers HTML for the vMCP."""
    request = httpx2.Request("POST", ENDPOINT)
    response = httpx2.Response(status, request=request, text="<html>502 Bad Gateway")
    return httpx2.HTTPStatusError(
        f"Server error '{status}'", request=request, response=response
    )


class _ReadingProxy(_ScriptedProxy):
    """A proxy whose tools are all reads — witan-code's shape."""

    def _writes(self, name):
        return False


@pytest.mark.parametrize("status", [502, 504])
def test_a_gateway_cutoff_on_a_write_is_indeterminate_not_unreachable(status):
    # ★ THE ORDERING TEST, second instance. Same trap as the 413: this is an
    # httpx2.HTTPStatusError and therefore an httpx2.HTTPError, so asking
    # `_transport_failure` first reports "could not be reached" about a service
    # that answered — and on a write that message invites the retry which
    # duplicates the row.
    proxy = _ScriptedProxy(
        ExceptionGroup("unhandled", [_gateway_error(status)]), at="call"
    )
    with pytest.raises(RemoteWriteIndeterminate) as caught:
        proxy.memory_store(content="x")
    message = str(caught.value)
    assert not isinstance(caught.value, RemoteUnreachable)
    assert "could not be reached" not in message
    assert "INDETERMINATE" in message
    assert f"HTTP {status}" in message
    assert "`memory_store`" in message
    assert "Re-read before retrying" in message


def test_a_gateway_cutoff_on_a_read_is_unreachable_but_says_it_was_reached():
    # Nothing was dispatched that could have changed anything, so the retry
    # advice is unqualified — but the old "could not be reached" was still
    # wrong about WHY, and sent readers to check an endpoint that is merely
    # saturated.
    proxy = _ReadingProxy(ExceptionGroup("unhandled", [_gateway_error(502)]), at="call")
    with pytest.raises(RemoteUnreachable) as caught:
        proxy.task_get(slug="x")
    message = str(caught.value)
    assert not isinstance(caught.value, RemoteWriteIndeterminate)
    assert "safe to retry" in message
    assert "saturated" in message


def test_an_unclassified_tool_is_assumed_to_write():
    """The default has to be the cautious one — see ``RemoteMCPProxy._writes``.

    A tool added to a server and not added to its read-only list gets an
    over-careful sentence. The inverse default would tell somebody their write
    did not happen when it may have, which is the mistake that costs data.
    """
    proxy = _ScriptedProxy(
        ExceptionGroup("unhandled", [_gateway_error(502)]), at="call"
    )
    with pytest.raises(RemoteWriteIndeterminate):
        proxy.task_get(slug="x")  # a read, but this proxy classifies nothing


def test_a_503_is_still_plainly_unreachable():
    """★ 503 is deliberately NOT in the gateway list.

    It means no upstream was available to try, so nothing was dispatched and
    nothing can have been written. Folding it in with 502 would relabel an
    unambiguous "did not happen" as an ambiguous "may have", which is a strict
    loss of information on exactly the calls that still have a safe answer.
    """
    proxy = _ScriptedProxy(
        ExceptionGroup("unhandled", [_gateway_error(503)]), at="call"
    )
    with pytest.raises(RemoteUnreachable) as caught:
        proxy.memory_store(content="x")
    assert not isinstance(caught.value, RemoteWriteIndeterminate)
    assert "could not be reached" in str(caught.value)


def test_a_gateway_cutoff_during_teardown_is_still_classified():
    # Same reason the 413 has this test: anyio re-raises a background failure
    # while the client is closing, and a write cut off there is exactly as
    # indeterminate as one cut off in the call.
    proxy = _ScriptedProxy(
        ExceptionGroup("unhandled", [_gateway_error(502)]), at="teardown"
    )
    with pytest.raises(RemoteWriteIndeterminate):
        proxy.memory_store(content="x")


def test_an_indeterminate_write_carries_the_underlying_error_as_its_cause():
    # The 502 on __cause__, the group on __context__ — see the 413 test above.
    inner = _gateway_error(502)
    group = ExceptionGroup("unhandled", [inner])
    proxy = _ScriptedProxy(group, at="call")
    with pytest.raises(RemoteWriteIndeterminate) as caught:
        proxy.memory_store(content="x")
    assert caught.value.__cause__ is inner
    assert caught.value.__context__ is group


def test_a_gateway_cut_read_carries_the_gateway_error_as_its_cause():
    # The read half of the same branch: it raises a different class, from a
    # different `raise` statement, so it needs its own assertion — the two
    # gateway raises are exactly the kind of near-duplicate that drifts.
    inner = _gateway_error(502)
    group = ExceptionGroup("unhandled", [inner])
    # _ReadingProxy, not _ScriptedProxy: an unclassified tool is assumed to
    # write (see test_an_unclassified_tool_is_assumed_to_write), which would
    # take the RemoteWriteIndeterminate raise instead of this one.
    proxy = _ReadingProxy(group, at="call")
    with pytest.raises(RemoteUnreachable) as caught:
        proxy.task_get(slug="x")
    assert caught.value.__cause__ is inner
    assert caught.value.__context__ is group


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


# ── a credential the deployment rejected ──────────────────────────────────
# OBSERVED LIVE 2026-08-13 against the CI deployment: 8 of 24 writers and
# several readers failed in a uniform ~13ms, which the pod log showed as
# `"POST /mcp HTTP/1.1" 401 Unauthorized`. The access token had expired mid-run
# — witan's refresh skew was 30s while a single write takes up to 51s, so the
# provider handed out a token that could not outlive the call it was fetched
# for (tk-the-oidc-refresh-skew-30s-is-shorter-than-a-writ-8b04db). These pin
# the client half: classify it honestly, and recover where recovery is safe.


def _auth_error(status: int) -> httpx2.HTTPStatusError:
    request = httpx2.Request("POST", ENDPOINT)
    response = httpx2.Response(status, request=request, text="Unauthorized")
    return httpx2.HTTPStatusError(
        f"Client error '{status}'", request=request, response=response
    )


class _RefreshingProxy(_ScriptedProxy):
    """Fails the first attempt with ``exc``; succeeds once refreshed.

    Models the real sequence rather than a single call: the point is that the
    SECOND attempt uses a different credential, so the test can assert the
    refresher was consulted instead of the cache handing back the same token.
    """

    def __init__(self, exc, *, at, writes=False):
        super().__init__(exc, at=at)
        self.tokens: list[str] = []
        self._writes_flag = writes
        self._refreshed = False
        self._token_refresher = self._refresh

    def _refresh(self, rejected):
        # The rejected credential is PASSED IN, not re-read from a cache that a
        # concurrent worker may already have replaced.
        self.rejected_seen = rejected
        self._refreshed = True
        return "fresh-token"

    def _writes(self, name):
        return self._writes_flag

    def _new_client(self, token):
        self.tokens.append(token)
        if self._refreshed:  # the retry: behave like a healthy deployment
            return _ScriptedProxy(None, at="never")._new_client(token)
        return super()._new_client(token)


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_credential_is_not_reported_as_unreachable(status):
    """★ THE ORDERING TEST, THIRD INSTANCE, and the most misleading fallout.

    A 401 is an httpx2.HTTPStatusError and therefore an httpx2.HTTPError, so
    asking `_transport_failure` first files an expired token under "could not
    be reached … check the endpoint" — sending somebody after DNS and ingress
    for something a refresh fixes.
    """
    proxy = _ScriptedProxy(
        ExceptionGroup("unhandled", [_auth_error(status)]), at="call"
    )
    with pytest.raises(RemoteCredentialRejected) as caught:
        proxy.task_get(slug="x")
    message = str(caught.value)
    assert not isinstance(caught.value, RemoteUnreachable)
    assert "could not be reached" not in message
    assert "rejected the credential" in message
    assert f"HTTP {status}" in message
    assert "re-authenticate" in message


def test_a_rejected_credential_carries_the_auth_error_as_its_cause():
    # Completes the set: every branch of _reclassifying now pins which
    # exception lands on __cause__, so the rule cannot drift one branch at a
    # time again.
    inner = _auth_error(401)
    group = ExceptionGroup("unhandled", [inner])
    proxy = _ScriptedProxy(group, at="call")
    with pytest.raises(RemoteCredentialRejected) as caught:
        proxy.task_get(slug="x")
    assert caught.value.__cause__ is inner
    assert caught.value.__context__ is group


def test_a_read_refreshes_once_and_retries():
    """The recoverable case, and the reason the whole thing exists: tokens live
    ~5 minutes, a slow run crosses an expiry boundary, and making the user
    re-issue the command by hand is a worse answer than refreshing."""
    proxy = _RefreshingProxy(
        ExceptionGroup("unhandled", [_auth_error(401)]), at="call", writes=False
    )
    proxy.task_get(slug="x")
    # The retry used the REFRESHED credential, not the cached one again.
    assert proxy.tokens == ["tok", "fresh-token"]


def test_a_write_is_never_retried_on_a_rejected_credential():
    """★ Same asymmetry the 502 path settled. The request reached the server,
    which may have applied it either side of judging the credential, so a blind
    retry writes the row twice whenever it did land."""
    proxy = _RefreshingProxy(
        ExceptionGroup("unhandled", [_auth_error(401)]), at="call", writes=True
    )
    with pytest.raises(RemoteCredentialRejected):
        proxy.memory_store(content="x")
    assert proxy.tokens == ["tok"], "a write must not be re-sent"


def test_without_a_refresher_a_rejected_credential_is_reported_not_retried():
    """A pinned-token client (the concurrency probe) has nothing to refresh to,
    and the message must say so rather than implying a refresh was tried."""
    proxy = _ScriptedProxy(ExceptionGroup("unhandled", [_auth_error(401)]), at="call")
    with pytest.raises(RemoteCredentialRejected) as caught:
        proxy.task_get(slug="x")
    assert "pinned credential" in str(caught.value)


def test_a_rejected_credential_at_CONNECT_is_classified_too():
    """★ The connect-time branch is separate code from `_reclassifying`, and the
    401 that motivated all of this arrives there — the client is built with the
    token, so a dead one fails while opening the session, not at `call_tool`.
    A call-time-only test leaves that path free to regress."""
    proxy = _ScriptedProxy(_auth_error(401), at="connect")
    with pytest.raises(RemoteCredentialRejected) as caught:
        proxy.task_get(slug="x")
    assert not isinstance(caught.value, RemoteUnreachable)
    assert "rejected the credential" in str(caught.value)


def test_a_read_rejected_at_CONNECT_refreshes_and_retries():
    proxy = _RefreshingProxy(_auth_error(401), at="connect", writes=False)
    proxy.task_get(slug="x")
    assert proxy.tokens == ["tok", "fresh-token"]


def test_the_refresher_is_given_the_credential_that_was_rejected():
    """Not left to re-read it: under concurrent 401s the cache may already hold
    somebody else's fresh token, and refreshing that spends a rotating refresh
    token for nothing."""
    proxy = _RefreshingProxy(
        ExceptionGroup("unhandled", [_auth_error(401)]), at="call", writes=False
    )
    proxy.task_get(slug="x")
    assert proxy.rejected_seen == "tok"


def test_a_write_says_no_refresh_was_attempted_rather_than_that_one_failed():
    """★ The message must report what HAPPENED, not what was configured. A
    write is re-raised without refreshing, so claiming the refresh already
    failed tells the reader the recovery is exhausted when it never began."""
    proxy = _RefreshingProxy(
        ExceptionGroup("unhandled", [_auth_error(401)]), at="call", writes=True
    )
    with pytest.raises(RemoteCredentialRejected) as caught:
        proxy.memory_store(content="x")
    message = str(caught.value)
    assert "No refresh was attempted" in message
    assert "INDETERMINATE" in message
    assert "A refresh was attempted" not in message


def test_a_read_whose_retry_is_also_rejected_says_the_refresh_failed():
    """The other half: once the refresh HAS run and the deployment still
    refuses, re-authenticating really is the next step."""

    class _AlwaysRejects(_RefreshingProxy):
        def _new_client(self, token):
            self.tokens.append(token)
            return _ScriptedProxy(self._exc, at=self._at)._new_client(token)

    proxy = _AlwaysRejects(
        ExceptionGroup("unhandled", [_auth_error(401)]), at="call", writes=False
    )
    with pytest.raises(RemoteCredentialRejected) as caught:
        proxy.task_get(slug="x")
    assert "A refresh was attempted" in str(caught.value)
    assert proxy.tokens == ["tok", "fresh-token"]
