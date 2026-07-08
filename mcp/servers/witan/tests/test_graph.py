"""Unit tests for OmnigraphClient._find_binary's lookup order.

Pure lookup-logic tests — no real omnigraph binary or query execution
involved (that's covered by the integration tests elsewhere, which skip
when the binary is absent).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from witan import graph as graph_mod
from witan.graph import OmnigraphClient, OmnigraphConflict


def test_find_binary_prefers_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    assert OmnigraphClient._find_binary() == "/usr/bin/omnigraph"


def test_find_binary_falls_back_to_local_bin_when_not_on_path(tmp_path, monkeypatch):
    """MCP servers launched by a desktop app/IDE extension often don't
    inherit a shell PATH — `witan setup` always installs here, so this
    fixed-path fallback is what makes them work out of the box."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    fallback = tmp_path / ".local" / "bin" / "omnigraph"
    fallback.parent.mkdir(parents=True)
    fallback.write_text("")

    assert OmnigraphClient._find_binary() == str(fallback)


def test_find_binary_raises_with_actionable_message_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="witan setup"):
        OmnigraphClient._find_binary()


# ── Optimistic-concurrency conflict surfacing (CAS support) ────────────


def _client(monkeypatch):
    """An OmnigraphClient over a remote URI (skips the local write lock),
    with the binary lookup stubbed so no real omnigraph is needed."""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    return OmnigraphClient("https://graph.example/g", Path("/queries"))


def _stub_run(monkeypatch, *, returncode, stderr):
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(graph_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(graph_mod.time, "sleep", lambda *_: None)
    return calls


def test_surface_conflict_raises_on_occ_conflict(monkeypatch):
    client = _client(monkeypatch)
    calls = _stub_run(monkeypatch, returncode=1, stderr="commit failed: stale view")

    with pytest.raises(OmnigraphConflict):
        client._execute(
            ["omnigraph", "mutate"], "mutate", is_write=True, surface_conflict=True
        )
    # surfaced immediately — no blind retry that would clobber the winner
    assert calls["n"] == 1


def test_conflict_is_retried_when_not_surfaced(monkeypatch):
    """Default behaviour is unchanged: a conflict is retried up to the cap
    (idempotent upserts rely on this)."""
    client = _client(monkeypatch)
    calls = _stub_run(monkeypatch, returncode=1, stderr="commit failed: stale view")

    with pytest.raises(RuntimeError, match="failed"):
        client._execute(["omnigraph", "mutate"], "mutate", is_write=True)
    assert calls["n"] > 1
