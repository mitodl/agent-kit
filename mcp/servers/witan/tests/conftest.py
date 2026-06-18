"""Shared fixtures for witan tests.

Integration tests spin up a throwaway omnigraph graph per test and point the
FastMCP server's client at it, so the real query/mutation files and the real
omnigraph binary are exercised end-to-end. Tests are skipped when the binary
is not on PATH.
"""

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


class _Tools:
    """Attribute proxy that returns unwrapped, directly-callable tools."""

    def __init__(self, module):
        self._module = module

    def __getattr__(self, name):
        return _unwrap(getattr(self._module, name))


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

    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import server as srv

    client = graph_mod.OmnigraphClient(str(store), cfg_mod.load().queries_dir)
    monkeypatch.setattr(srv, "client", client)
    return _Tools(srv)
