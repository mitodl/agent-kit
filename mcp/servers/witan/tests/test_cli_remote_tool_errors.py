"""A server-side refusal on a remote target must read as a sentence.

Found during the first live cutover: `WITAN_TARGET=ci witan migrate merge …`
against a tool that refused printed ~40 lines of cyclopts → asyncio → fastmcp
internals with the actual message on the last one. `fastmcp.exceptions.ToolError`
is not a `RuntimeError` (`ToolError → FastMCPError → Exception`), so it sailed
straight past the `except RuntimeError` every CLI command wraps its call in.

These tests drive a REAL `RemoteServerProxy` over a scripted client rather than
a fake server object, because the defect lived in the seam between the two: a
stub that raises `RemoteToolFailed` directly would pass even with the proxy's
classification removed.
"""

from __future__ import annotations

import pytest


def _refusing_proxy(message: str):
    """A `RemoteServerProxy` whose every tool call is refused server-side."""
    from fastmcp.exceptions import ToolError
    from witan.config import RemoteConfig
    from witan.remote.proxy import RemoteServerProxy

    cfg = RemoteConfig(url="https://witan.example/mcp", oidc_issuer="https://sso/ol")
    proxy = RemoteServerProxy(cfg, lambda: "tok")
    # Seeded so the call reaches call_tool without a tools/list round trip.
    proxy._param_names = {
        "store_merge": ["rows", "dry_run"],
        "task_ready": ["repo", "limit"],
    }

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def call_tool(self, name, arguments):
            raise ToolError(message)

    proxy._new_client = lambda token: _Client()  # type: ignore[method-assign]
    return proxy


def _capture(monkeypatch, proxy):
    from witan.cli import _common

    monkeypatch.setattr(_common, "_server", proxy)
    printed: list[str] = []
    monkeypatch.setattr(
        _common.console, "print", lambda *a, **kw: printed.append(str(a[0]))
    )
    return printed


@pytest.fixture
def export(tmp_path):
    """A one-row `omnigraph export` file, so the merge reaches the remote call.

    A `.jsonl` source is passed straight through; a store URI would instead
    shell out to `omnigraph export` and fail on the store, never getting as far
    as the tool call under test.
    """
    path = tmp_path / "source.jsonl"
    path.write_text('{"type": "Memory", "slug": "mem-x", "title": "x"}\n')
    return str(path)


def test_a_refused_merge_reads_as_one_line_not_a_traceback(monkeypatch, export):
    # THE OBSERVED FAILURE, on the command that produced the report:
    # `WITAN_TARGET=ci witan migrate merge <store> --dry-run`. `_merge` has its
    # own `except RuntimeError` — the point is that the refusal now reaches it
    # instead of sailing past as a ToolError.
    from witan.cli.migrate import _merge

    message = "cannot merge: source graph has no __manifest"
    printed = _capture(monkeypatch, _refusing_proxy(message))

    with pytest.raises(SystemExit) as exit_code:
        _merge(export, target=None, dry_run=True)

    assert exit_code.value.code == 1
    assert len(printed) == 1
    assert message in printed[0]


def test_the_refusal_carries_the_servers_own_words(monkeypatch, export):
    # The message was never missing — it was buried under the traceback. A
    # handler that rendered a generic "the remote call failed" would satisfy
    # "one line" and still lose the only useful part.
    from witan.cli.migrate import _merge

    printed = _capture(monkeypatch, _refusing_proxy("edge row 1: unknown type Tagged"))

    with pytest.raises(SystemExit):
        _merge(export, target=None, dry_run=False)

    assert "edge row 1: unknown type Tagged" in printed[0]


def test_a_command_with_no_handler_of_its_own_is_caught_by_the_entrypoint(monkeypatch):
    # THE WIDER CLASS. Only `migrate` has an `except RuntimeError`; every other
    # command — memory, tasks, projects, traces — relies on `main()`'s net. A
    # Cedar denial is the common way to reach it on a shared deployment.
    from witan.remote.proxy import RemoteToolFailed

    proxy = _refusing_proxy("cedar: read denied on graph 'council'")
    from witan.cli._common import _fn

    with pytest.raises(RemoteToolFailed, match="cedar: read denied"):
        _fn(proxy.task_ready)(repo="", limit=5)


def test_the_entrypoint_actually_lists_the_type_it_must_catch():
    # Pins the wiring the test above cannot: that `main()` names
    # RemoteToolFailed, so the exception it raises is rendered rather than
    # escaping as the traceback this whole change exists to remove.
    import inspect

    from witan.cli import main

    assert "RemoteToolFailed" in inspect.getsource(main)


def test_a_refusal_is_a_runtime_error_so_existing_handlers_keep_working():
    # The property that made this a four-line fix instead of a thirty-call-site
    # one, and the property a future refactor could silently drop.
    from witan.remote.proxy import RemoteToolFailed

    assert issubclass(RemoteToolFailed, RuntimeError)
