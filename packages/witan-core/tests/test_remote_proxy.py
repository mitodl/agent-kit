"""RemoteMCPProxy generic dispatch/arg-mapping/policy hooks (witan_core.remote).

The end-to-end "dispatch a real tool call over an in-memory FastMCP server" test
lives in witan-council (it needs witan's server + tools). Here we exercise the
transport-agnostic mechanism in isolation: positional→name mapping, the repo
resolver hook, the None-dropping, and the admin/unknown refusal wording.
"""

from __future__ import annotations

import pytest

from witan_core.remote.proxy import RemoteMCPProxy, RemoteToolUnavailable


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
