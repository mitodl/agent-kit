"""Store-compaction throttling + optimize/cleanup wrappers."""

import subprocess

import pytest

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
    assert maintenance.spawn_background_optimize(store, now=1_000_000.0) is True
    assert maintenance.spawn_background_optimize(store, now=1_000_000.0 + 5) is False
    assert len(calls) == 1
    argv, kwargs = calls[0]
    # never cleaned up before, so the first run also cleans up
    assert argv[1:] == [
        "-m",
        "witan",
        "optimize",
        "--store",
        store,
        "--cleanup-older-than",
        maintenance.CLEANUP_OLDER_THAN,
    ]
    assert kwargs["start_new_session"] is True

    # after the optimize window elapses it spawns again, without the cleanup,
    # whose weekly window has not
    assert maintenance.spawn_background_optimize(store, now=1_000_000.0 + 4000) is True
    assert len(calls) == 2
    assert "--cleanup-older-than" not in calls[1][0]


def test_cleanup_rides_optimize_on_its_own_window(monkeypatch, tmp_path):
    """omnigraph 0.11 reclaims deleted branches' storage only in `cleanup`, so
    the throttled optimize has to run one now and then or local stores grow
    without bound."""
    from witan import maintenance

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("WITAN_OPTIMIZE_INTERVAL", "3600")
    monkeypatch.setenv("WITAN_CLEANUP_INTERVAL", "86400")
    store = str(tmp_path / "g.omni")

    calls = []
    monkeypatch.setattr(
        maintenance.subprocess, "Popen", lambda argv, **kw: calls.append(argv)
    )

    for hour in range(26):
        maintenance.spawn_background_optimize(store, now=100_000.0 + hour * 3600)
    cleaned = ["--cleanup-older-than" in argv for argv in calls]
    assert len(calls) == 26
    assert cleaned.count(True) == 2
    assert cleaned[0] and cleaned[24]


def test_cleanup_disabled_leaves_optimize_alone(monkeypatch, tmp_path):
    from witan import maintenance

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("WITAN_OPTIMIZE_INTERVAL", "3600")
    monkeypatch.setenv("WITAN_CLEANUP_INTERVAL", "0")
    store = str(tmp_path / "g.omni")

    calls = []
    monkeypatch.setattr(
        maintenance.subprocess, "Popen", lambda argv, **kw: calls.append(argv)
    )

    assert maintenance.spawn_background_optimize(store, now=50_000.0) is True
    assert "--cleanup-older-than" not in calls[0]


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


def test_cli_cleanup_with_no_bound_keeps_the_hooks_window(tmp_path, monkeypatch):
    """From omnigraph 0.12 `--keep` counts graph commits, so a default of
    `--keep 10` would cut a store to its last ten writes. No bound means the
    age window the Stop hook already uses."""
    from witan import maintenance
    from witan.cli import maintenance as cli_maint

    store = tmp_path / "s.omni"
    store.mkdir()
    printed = []
    calls = []

    class _Client:
        def cleanup(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(
        cli_maint.console, "print", lambda *a, **k: printed.append(str(a[0]))
    )
    monkeypatch.setattr(cli_maint, "_client", lambda _uri: _Client())

    cli_maint.cleanup(store=str(store))
    assert f"newer than {maintenance.CLEANUP_OLDER_THAN}" in printed[-1]
    assert calls == []

    cli_maint.cleanup(store=str(store), yes=True)
    cli_maint.cleanup(store=str(store), keep=3, yes=True)
    assert calls == [
        {"keep": None, "older_than": maintenance.CLEANUP_OLDER_THAN},
        {"keep": 3, "older_than": None},
    ]


@pytest.mark.parametrize("command", ["optimize", "cleanup"])
def test_cli_maintenance_refuses_a_deployed_graph(monkeypatch, command):
    """Both are direct-storage commands, and from omnigraph 0.12 the server's
    `serve` lock refuses them anyway. Say so instead of running the CLI into
    its own error."""
    from witan.cli import maintenance as cli_maint

    printed = []
    monkeypatch.setattr(
        cli_maint.console, "print", lambda *a, **k: printed.append(str(a[0]))
    )
    monkeypatch.setattr(
        cli_maint, "_client", lambda _uri: pytest.fail("must not build a client")
    )

    getattr(cli_maint, command)(store="https://omnigraph.test")

    assert "serve" in printed[-1] and "Nothing to do" in printed[-1]


@requires_omnigraph
def test_cli_optimize_then_cleanup(tmp_path, monkeypatch):
    from witan.cli import maintenance as cli_maint

    store = _fresh_store(tmp_path)
    printed = []
    monkeypatch.setattr(
        cli_maint.console, "print", lambda *a, **k: printed.append(str(a[0]))
    )

    cli_maint.optimize(store=str(store), cleanup_older_than="30d")
    assert any("Optimized and cleaned up" in p for p in printed)


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
