"""Unit tests for OmnigraphClient._find_binary's lookup order.

Pure lookup-logic tests — no real omnigraph binary or query execution
involved (that's covered by the integration tests elsewhere, which skip
when the binary is absent). Mirrors witan/tests/test_graph.py.
"""

import shutil
from pathlib import Path

import pytest

from witan_code.graph import OmnigraphClient


def test_find_binary_prefers_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    assert OmnigraphClient._find_binary() == "/usr/bin/omnigraph"


def test_find_binary_falls_back_to_local_bin_when_not_on_path(tmp_path, monkeypatch):
    """MCP servers launched by a desktop app/IDE extension often don't
    inherit a shell PATH — `witan-code setup`/`witan setup` always install
    here, so this fixed-path fallback is what makes them work out of the box."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    fallback = tmp_path / ".local" / "bin" / "omnigraph"
    fallback.parent.mkdir(parents=True)
    fallback.write_text("")

    assert OmnigraphClient._find_binary() == str(fallback)


def test_find_binary_raises_with_actionable_message_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="witan-code setup"):
        OmnigraphClient._find_binary()
