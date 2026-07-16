"""Unit tests for the shared OmnigraphClient base.

Covers _find_binary lookup order, optimistic-concurrency conflict surfacing,
and the per-actor admission-cap backoff — the LOCAL/REMOTE-generic machinery.
Each server's own test_graph.py keeps only its subclass-specific bits (the
setup-hint message; witan-code's branch ops; witan's apply_schema).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from witan_core import omnigraph as og
from witan_core.omnigraph import OmnigraphClient, OmnigraphConflict


def test_find_binary_prefers_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    assert OmnigraphClient._find_binary() == "/usr/bin/omnigraph"


def test_find_binary_falls_back_to_local_bin_when_not_on_path(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    fallback = tmp_path / ".local" / "bin" / "omnigraph"
    fallback.parent.mkdir(parents=True)
    fallback.write_text("")

    assert OmnigraphClient._find_binary() == str(fallback)


def test_find_binary_raises_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="omnigraph binary not found"):
        OmnigraphClient._find_binary()


def test_is_storage_version_mismatch_detects_marker_pair():
    msg = "graph is stamped at internal schema 5 but this binary reads only 4"
    assert og._is_storage_version_mismatch(msg) is True
    assert og._is_storage_version_mismatch("some other omnigraph error") is False


# ── conflict surfacing (CAS support) ──────────────────────────────


def _client(monkeypatch):
    """A base client over a remote URI (skips the local write lock), with the
    binary lookup stubbed so no real omnigraph is needed."""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    return OmnigraphClient("https://graph.example/g", Path("/queries"))


def _stub_run(monkeypatch, *, returncode, stderr):
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(og.subprocess, "run", fake_run)
    monkeypatch.setattr(og.time, "sleep", lambda *_: None)
    return calls


def test_surface_conflict_raises_on_occ_conflict(monkeypatch):
    client = _client(monkeypatch)
    calls = _stub_run(monkeypatch, returncode=1, stderr="commit failed: stale view")

    with pytest.raises(OmnigraphConflict):
        client._execute(
            ["omnigraph", "mutate"], "mutate", is_write=True, surface_conflict=True
        )
    assert calls["n"] == 1  # surfaced immediately, no clobbering retry


def test_conflict_is_retried_when_not_surfaced(monkeypatch):
    client = _client(monkeypatch)
    calls = _stub_run(monkeypatch, returncode=1, stderr="commit failed: stale view")

    with pytest.raises(RuntimeError, match="failed"):
        client._execute(["omnigraph", "mutate"], "mutate", is_write=True)
    assert calls["n"] > 1


# ── per-actor admission cap backoff ───────────────────────────────


def test_admission_cap_backoff_adds_bounded_jitter():
    delay = og._ADMISSION_CAP_BASE_DELAY * (2 ** (3 - 1))  # attempt=3
    for _ in range(50):
        backoff = og._admission_cap_backoff(3)
        assert delay <= backoff <= delay * 1.1
    assert og._admission_cap_backoff(20) == og._ADMISSION_CAP_MAX_DELAY


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

    monkeypatch.setattr(og.subprocess, "run", fake_run)
    monkeypatch.setattr(og.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(og.random, "uniform", lambda a, b: 0.0)  # no jitter

    out = client._execute(["omnigraph", "mutate"], "mutate", is_write=True)
    assert out == "ok"
    assert calls["n"] == 3
    assert sleeps == [0.25, 0.5]  # base 0.25s, doubling — independent oracle


def test_byte_budget_exceeded_also_retries(monkeypatch):
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

    monkeypatch.setattr(og.subprocess, "run", fake_run)
    monkeypatch.setattr(og.time, "sleep", lambda *_: None)

    out = client._execute(["omnigraph", "load"], "load", is_write=True)
    assert out == "ok"
    assert calls["n"] == 2


def test_inflight_cap_gives_up_after_its_own_budget(monkeypatch):
    client = _client(monkeypatch)
    calls = _stub_run(
        monkeypatch, returncode=1, stderr="actor in-flight count cap 16 exceeded"
    )

    with pytest.raises(RuntimeError, match="admission cap exceeded"):
        client._execute(["omnigraph", "mutate"], "mutate", is_write=True)
    assert calls["n"] == og._ADMISSION_CAP_MAX_ATTEMPTS


def test_inflight_cap_ignores_surface_conflict(monkeypatch):
    client = _client(monkeypatch)
    _stub_run(monkeypatch, returncode=1, stderr="actor in-flight count cap 16 exceeded")

    with pytest.raises(RuntimeError, match="admission cap exceeded"):
        client._execute(
            ["omnigraph", "mutate"], "mutate", is_write=True, surface_conflict=True
        )
