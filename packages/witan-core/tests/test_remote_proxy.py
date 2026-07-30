"""RemoteMCPProxy generic dispatch/arg-mapping/policy hooks (witan_core.remote).

The end-to-end "dispatch a real tool call over an in-memory FastMCP server" test
lives in witan-council (it needs witan's server + tools). Here we exercise the
transport-agnostic mechanism in isolation: positional→name mapping, the repo
resolver hook, the None-dropping, and the admin/unknown refusal wording.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from witan_core.remote.proxy import (
    RemoteMCPProxy,
    RemoteToolUnavailable,
    _tool_input_schema,
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
        self._param_names = {
            "task_get": ["slug"],
            "task_ready": ["repo"],
            "task_create": ["title", "description", "repo"],
            "memory_store": ["kind", "title", "content", "repo", "session_slug"],
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


def test_positional_arg_maps_to_param_name():
    p = _Proxy()
    assert p._map_args("task_get", ("s-1",), {}) == {"slug": "s-1"}


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
    out = p._map_args("memory_store", ("lesson", "t", "c"), {})
    assert out["session_slug"] == "ws-abc"


def test_explicit_session_slug_is_not_overwritten():
    p = _Proxy(session="ws-ambient")
    out = p._map_args("memory_store", ("lesson", "t", "c"), {"session_slug": "ws-mine"})
    assert out["session_slug"] == "ws-mine"


def test_session_slug_dropped_when_no_active_session():
    p = _Proxy(session=None)
    assert "session_slug" not in p._map_args("memory_store", ("lesson", "t", "c"), {})


def test_session_slug_not_added_to_tools_without_the_param():
    p = _Proxy(session="ws-abc")
    assert "session_slug" not in p._map_args("task_ready", (), {"repo": ""})


def test_none_optionals_are_dropped():
    p = _Proxy()
    out = p._map_args("task_create", ("t",), {"description": None, "repo": "r"})
    assert out == {"title": "t", "repo": "r"}


def test_unknown_tool_raises_with_hook_message():
    p = _Proxy()
    with pytest.raises(RemoteToolUnavailable, match="no such tool: nope"):
        p._map_args("nope", (), {})


def test_too_many_positionals_raises_not_indexerror():
    # task_get has one param; two positionals is a client/server signature
    # mismatch that must surface as RemoteToolUnavailable, not IndexError.
    p = _Proxy()
    with pytest.raises(RemoteToolUnavailable, match="positional"):
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
