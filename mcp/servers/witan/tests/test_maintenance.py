"""Store-compaction throttling + optimize/cleanup wrappers."""

import subprocess

from .conftest import SCHEMA, requires_omnigraph


# ── throttle logic (no omnigraph needed) ─────────────────────────────────────


def test_optimize_interval_env_override(monkeypatch):
    from witan import maintenance

    monkeypatch.delenv("WITAN_OPTIMIZE_INTERVAL", raising=False)
    assert maintenance.optimize_interval() == maintenance._OPTIMIZE_INTERVAL
    monkeypatch.setenv("WITAN_OPTIMIZE_INTERVAL", "3600")
    assert maintenance.optimize_interval() == 3600.0
    monkeypatch.setenv("WITAN_OPTIMIZE_INTERVAL", "0")
    assert maintenance.optimize_interval() == 0.0
    monkeypatch.setenv("WITAN_OPTIMIZE_INTERVAL", "junk")
    assert maintenance.optimize_interval() == maintenance._OPTIMIZE_INTERVAL


def test_due_respects_interval_disabled_and_remote(monkeypatch, tmp_path):
    from witan import maintenance

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    store = str(tmp_path / "g.omni")

    # disabled
    monkeypatch.setenv("WITAN_OPTIMIZE_INTERVAL", "0")
    assert maintenance.due(store) is False

    # remote stores are maintained server-side, never by the client hook
    monkeypatch.setenv("WITAN_OPTIMIZE_INTERVAL", "3600")
    assert maintenance.due("https://example.com/graph") is False

    # never run before → due; just-run → not due
    assert maintenance.due(store, now=10_000.0) is True
    maintenance._mark_run(store, 10_000.0)
    assert maintenance.due(store, now=10_000.0 + 100) is False
    assert maintenance.due(store, now=10_000.0 + 4000) is True


def test_spawn_background_optimize_throttles(monkeypatch, tmp_path):
    from witan import maintenance

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("WITAN_OPTIMIZE_INTERVAL", "3600")
    store = str(tmp_path / "g.omni")

    calls = []

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))

    monkeypatch.setattr(maintenance.subprocess, "Popen", _FakePopen)

    # first call spawns and stamps; second (within window) is throttled
    assert maintenance.spawn_background_optimize(store, now=50_000.0) is True
    assert maintenance.spawn_background_optimize(store, now=50_000.0 + 5) is False
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[1:] == ["-m", "witan", "optimize", "--store", store]
    assert kwargs["start_new_session"] is True

    # after the window elapses it spawns again
    assert maintenance.spawn_background_optimize(store, now=50_000.0 + 4000) is True
    assert len(calls) == 2


def test_spawn_marks_before_spawn_so_failure_does_not_hotloop(monkeypatch, tmp_path):
    from witan import maintenance

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("WITAN_OPTIMIZE_INTERVAL", "3600")
    store = str(tmp_path / "g.omni")

    def _boom(*a, **k):
        raise OSError("cannot spawn")

    monkeypatch.setattr(maintenance.subprocess, "Popen", _boom)

    # spawn fails but returns False (not raises); the stamp was still written so
    # the next stop within the window won't retry.
    assert maintenance.spawn_background_optimize(store, now=60_000.0) is False
    assert maintenance.due(store, now=60_000.0 + 5) is False


# ── optimize/cleanup actually run against a real store ───────────────────────


def _fresh_store(tmp_path):
    store = tmp_path / "graph.omni"
    subprocess.run(
        ["omnigraph", "init", "--schema", str(SCHEMA), str(store)],
        check=True,
        capture_output=True,
        text=True,
    )
    return store


@requires_omnigraph
def test_client_optimize_runs(tmp_path):
    from witan import config as cfg_mod
    from witan.graph import OmnigraphClient

    store = _fresh_store(tmp_path)
    client = OmnigraphClient(str(store), cfg_mod.load().queries_dir)
    # non-destructive, idempotent — just assert it completes without raising
    client.optimize()


@requires_omnigraph
def test_client_cleanup_requires_a_bound(tmp_path):
    import pytest

    from witan import config as cfg_mod
    from witan.graph import OmnigraphClient

    store = _fresh_store(tmp_path)
    client = OmnigraphClient(str(store), cfg_mod.load().queries_dir)
    with pytest.raises(ValueError):
        client.cleanup()
    # with a bound it runs
    client.cleanup(keep=5)


@requires_omnigraph
def test_cli_optimize_and_cleanup(tmp_path, monkeypatch):
    from witan.cli import maintenance as cli_maint

    store = _fresh_store(tmp_path)
    printed = []
    monkeypatch.setattr(
        cli_maint.console, "print", lambda *a, **k: printed.append(str(a[0]))
    )

    cli_maint.optimize(store=str(store))
    assert any("Optimized" in p for p in printed)

    printed.clear()
    # without --yes, cleanup refuses (destructive)
    cli_maint.cleanup(store=str(store), keep=3)
    assert any("--yes" in p for p in printed)

    printed.clear()
    cli_maint.cleanup(store=str(store), keep=3, yes=True)
    assert any("Cleaned up" in p for p in printed)


def test_cli_optimize_missing_store_is_noop(tmp_path, monkeypatch):
    from witan.cli import maintenance as cli_maint

    printed = []
    monkeypatch.setattr(
        cli_maint.console, "print", lambda *a, **k: printed.append(str(a[0]))
    )
    cli_maint.optimize(store=str(tmp_path / "does-not-exist.omni"))
    assert any("nothing to do" in p.lower() for p in printed)


def test_resolve_store_expands_user(tmp_path, monkeypatch):
    from witan.cli import maintenance as cli_maint

    # A `--store ~/…` path is expanded before the existence check, so an existing
    # store under HOME resolves instead of being treated as missing.
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "g.omni").mkdir()
    resolved = cli_maint._resolve_store("~/g.omni")
    assert resolved == str(tmp_path / "g.omni")
    assert "~" not in resolved


def test_mark_run_atomic_roundtrip(tmp_path, monkeypatch):
    from witan import maintenance

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    store = str(tmp_path / "g.omni")
    maintenance._mark_run(store, 12345.0)
    assert maintenance._last_run(store) == 12345.0
    # no leftover temp files from the atomic write
    assert not list(maintenance.session_state.session_state_dir().glob("*.tmp"))
