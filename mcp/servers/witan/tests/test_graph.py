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


# ── Per-actor admission cap backoff (in-flight count + byte budget) ────


def test_admission_cap_backoff_adds_bounded_jitter():
    """Jitter breaks lockstep retries from a synchronized burst — bounded to
    10% of the base delay, and the schedule still caps at max_delay."""
    delay = graph_mod._ADMISSION_CAP_BASE_DELAY * (2 ** (3 - 1))  # attempt=3
    for _ in range(50):
        backoff = graph_mod._admission_cap_backoff(3)
        assert delay <= backoff <= delay * 1.1

    assert graph_mod._admission_cap_backoff(20) == graph_mod._ADMISSION_CAP_MAX_DELAY


def test_inflight_cap_retries_then_succeeds(monkeypatch):
    client = _client(monkeypatch)
    calls = {"n": 0}
    sleeps = []

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="actor in-flight count cap 16 exceeded"
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(graph_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(graph_mod.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(graph_mod.random, "uniform", lambda a, b: 0.0)  # no jitter

    out = client._execute(["omnigraph", "mutate"], "mutate", is_write=True)
    assert out == "ok"
    assert calls["n"] == 3
    # concrete expected delays (base 0.25s, doubling), not derived from the
    # implementation's own helper — an independent oracle on the schedule
    assert sleeps == [0.25, 0.5]


def test_byte_budget_exceeded_also_retries(monkeypatch):
    """The sibling admission-cap rejection (byte budget, not in-flight
    count) is the same underlying WorkloadController mechanism and gets
    the same retry treatment."""
    client = _client(monkeypatch)
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] < 2:
            return subprocess.CompletedProcess(
                cmd,
                1,
                stdout="",
                stderr="actor byte budget exceeded: would use 999 bytes against cap 100",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(graph_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(graph_mod.time, "sleep", lambda *_: None)

    out = client._execute(["omnigraph", "load"], "load", is_write=True)
    assert out == "ok"
    assert calls["n"] == 2


def test_inflight_cap_gives_up_after_its_own_budget(monkeypatch):
    """The admission cap has its own retry budget, independent of the
    general _MAX_ATTEMPTS used for stale-view/drift retries."""
    client = _client(monkeypatch)
    calls = _stub_run(
        monkeypatch, returncode=1, stderr="actor in-flight count cap 16 exceeded"
    )

    with pytest.raises(RuntimeError, match="admission cap exceeded"):
        client._execute(["omnigraph", "mutate"], "mutate", is_write=True)
    assert calls["n"] == graph_mod._ADMISSION_CAP_MAX_ATTEMPTS


def test_inflight_cap_ignores_surface_conflict(monkeypatch):
    """Unlike a stale-view OCC conflict, the admission cap isn't a
    compare-and-swap race — surface_conflict=True must not shortcut it to
    OmnigraphConflict."""
    client = _client(monkeypatch)
    _stub_run(monkeypatch, returncode=1, stderr="actor in-flight count cap 16 exceeded")

    with pytest.raises(RuntimeError, match="admission cap exceeded"):
        client._execute(
            ["omnigraph", "mutate"], "mutate", is_write=True, surface_conflict=True
        )
