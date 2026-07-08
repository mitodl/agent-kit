"""Shared fixtures for witan tests.

Integration tests spin up a throwaway omnigraph graph per test and point the
FastMCP server's client at it, so the real query/mutation files and the real
omnigraph binary are exercised end-to-end. Tests are skipped when the binary
is not on PATH.
"""

import asyncio
import inspect
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema" / "schema.pg"

omnigraph_available = shutil.which("omnigraph") is not None
requires_omnigraph = pytest.mark.skipif(
    not omnigraph_available, reason="omnigraph binary not on PATH"
)


def _unwrap(tool):
    """Return the underlying function for a FastMCP-decorated tool."""
    return getattr(tool, "fn", tool)


class _NoElicitCtx:
    """A stand-in Context whose ``elicit`` always errors — simulates a client
    without elicitation support, so async tools fall back to their default
    (non-interactive) behavior. Tests that exercise elicitation pass their own
    fake ctx via ``ctx=...`` instead."""

    async def elicit(self, *args, **kwargs):
        raise RuntimeError("elicitation unsupported in tests")


class _Tools:
    """Attribute proxy that returns unwrapped, directly-callable tools.

    Async tools (those taking a ``ctx: Context``) are run to completion via
    ``asyncio.run`` with a no-elicit ctx injected, so the 50+ existing sync call
    sites keep working unchanged and get today's non-interactive behavior."""

    def __init__(self, module):
        self._module = module

    def __getattr__(self, name):
        fn = _unwrap(getattr(self._module, name))
        if inspect.iscoroutinefunction(fn):

            def runner(*args, **kwargs):
                kwargs.setdefault("ctx", _NoElicitCtx())
                return asyncio.run(fn(*args, **kwargs))

            return runner
        return fn


@pytest.fixture
def server(tmp_path, monkeypatch):
    if not omnigraph_available:
        pytest.skip("omnigraph binary not on PATH")

    store = tmp_path / "graph.omni"
    subprocess.run(
        ["omnigraph", "init", "--schema", str(SCHEMA), str(store)],
        check=True,
        capture_output=True,
        text=True,
    )

    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/repo")
    monkeypatch.setenv("WITAN_AUTHOR", "pytest")
    # Isolate from the real agent session: memory_store auto-wires a
    # SessionProduced edge when CLAUDE_SESSION_ID resolves to a live session.
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import server as srv

    client = graph_mod.OmnigraphClient(str(store), cfg_mod.load().queries_dir)
    monkeypatch.setattr(srv, "client", client)
    return _Tools(srv)
