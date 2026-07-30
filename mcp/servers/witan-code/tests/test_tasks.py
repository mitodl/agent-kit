"""`code_reindex` under the io.modelcontextprotocol/tasks extension (SEP-2663).

A full rebuild runs for minutes on a large repo, so a client can ask for the
call to run as a background task and poll it. The extension is an optional
dependency (`witan-code[tasks]`), and the declaration is gated on it — a
`task=True` tool with no extension registered makes the server refuse every
connection, so the gate is what keeps the extra optional rather than required.
"""

import asyncio

import pytest

from .conftest import requires_stack

pytest.importorskip("fastmcp_tasks", reason="requires the `tasks` extra")


def _index(sample_repo, monkeypatch, *, as_task):
    """Reindex ``sample_repo`` through a real MCP client, sync or tasked."""
    from fastmcp import Client
    from fastmcp_tasks import call_tool_task

    from witan_code import config as cfg_mod
    from witan_code import server as srv

    # server.cfg was captured at import; refresh it for the test's env + store.
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()

    async def _call():
        async with Client(srv.mcp, mode="2026-07-28") as client:
            arguments = {"path": str(sample_repo)}
            if not as_task:
                return (await client.call_tool("code_reindex", arguments)).data
            handle = await call_tool_task(client, "code_reindex", arguments)
            # The point of the extension: the handle comes back before the
            # index does, so the client is free while the work runs.
            assert (await handle.status()).task_id
            return (await handle.result()).data

    return asyncio.run(_call())


def test_extension_is_registered_when_installed():
    # The import gate silently turning the extension off would leave every
    # reindex synchronous with nothing else failing, so assert it directly.
    from witan_code import server as srv

    assert srv.TASKS_ENABLED


def test_server_still_serves_without_the_extra(monkeypatch):
    """The default install has no `fastmcp_tasks`, and must still start.

    Worth its own test because the failure is total rather than degraded: a
    `task=True` tool whose extension is missing makes FastMCP reject every
    connection, so a gate that stopped tracking the import would take the whole
    server down for anyone who didn't install the extra.
    """
    import builtins
    import importlib

    from witan_code import server as srv

    real_import = builtins.__import__

    def _without_tasks(name, *args, **kwargs):
        if name == "fastmcp_tasks":
            raise ImportError("masked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _without_tasks)
    try:
        importlib.reload(srv)
        assert srv.TASKS_ENABLED is False

        async def _connect():
            from fastmcp import Client

            async with Client(srv.mcp, mode="2026-07-28") as client:
                return await client.list_tools()

        assert any(t.name == "code_reindex" for t in asyncio.run(_connect()))
    finally:
        # Other tests hold references through this module; put the real one back.
        monkeypatch.undo()
        importlib.reload(srv)


@requires_stack
def test_reindex_runs_as_a_task(sample_repo, monkeypatch):
    stats = _index(sample_repo, monkeypatch, as_task=True)
    assert stats["symbols"] >= 3
    assert stats["errors"] == 0


@requires_stack
def test_reindex_still_answers_synchronously(sample_repo, monkeypatch):
    # Task execution is the client's choice, not the server's: a client that
    # doesn't ask must get the same completed result it always did.
    stats = _index(sample_repo, monkeypatch, as_task=False)
    assert stats["symbols"] >= 3
    assert stats["errors"] == 0
