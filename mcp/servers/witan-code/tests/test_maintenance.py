"""Store-compaction throttling + optimize/cleanup wrappers.

Mirrors mcp/servers/witan/tests/test_maintenance.py's coverage of the
equivalent witan functions — the two are deliberately duplicated (no
cross-package import, see maintenance.py's docstring), so their tests are
duplicated too.
"""

import subprocess

from .conftest import requires_omnigraph

# ── throttle logic (no omnigraph needed) ─────────────────────────────────────


def test_optimize_interval_env_override(monkeypatch):
    from witan_code import maintenance

    monkeypatch.delenv("WITAN_CODE_OPTIMIZE_INTERVAL", raising=False)
    assert maintenance.optimize_interval() == maintenance._OPTIMIZE_INTERVAL
    monkeypatch.setenv("WITAN_CODE_OPTIMIZE_INTERVAL", "3600")
    assert maintenance.optimize_interval() == 3600.0
    monkeypatch.setenv("WITAN_CODE_OPTIMIZE_INTERVAL", "0")
    assert maintenance.optimize_interval() == 0.0
    monkeypatch.setenv("WITAN_CODE_OPTIMIZE_INTERVAL", "junk")
    assert maintenance.optimize_interval() == maintenance._OPTIMIZE_INTERVAL


def test_due_respects_interval_disabled_remote_and_missing(monkeypatch, tmp_path):
    from witan_code import maintenance

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    store = tmp_path / "g.omni"

    # missing store — nothing to compact yet
    monkeypatch.setenv("WITAN_CODE_OPTIMIZE_INTERVAL", "3600")
    assert maintenance.due(store) is False

    store.mkdir()

    # disabled
    monkeypatch.setenv("WITAN_CODE_OPTIMIZE_INTERVAL", "0")
    assert maintenance.due(store) is False

    # remote stores are maintained server-side, never by the client hook
    monkeypatch.setenv("WITAN_CODE_OPTIMIZE_INTERVAL", "3600")
    assert maintenance.due("https://example.com/graph") is False

    # never run before → due; just-run → not due
    assert maintenance.due(store, now=10_000.0) is True
    maintenance._mark_run(store, 10_000.0)
    assert maintenance.due(store, now=10_000.0 + 100) is False
    assert maintenance.due(store, now=10_000.0 + 4000) is True


def test_spawn_background_optimize_throttles(monkeypatch, tmp_path):
    from witan_code import maintenance

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("WITAN_CODE_OPTIMIZE_INTERVAL", "3600")
    store = tmp_path / "g.omni"
    store.mkdir()

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
    assert argv[1:] == ["-m", "witan_code", "optimize", "--store", str(store)]
    assert kwargs["start_new_session"] is True

    # after the window elapses it spawns again
    assert maintenance.spawn_background_optimize(store, now=50_000.0 + 4000) is True
    assert len(calls) == 2


def test_spawn_marks_before_spawn_so_failure_does_not_hotloop(monkeypatch, tmp_path):
    from witan_code import maintenance

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("WITAN_CODE_OPTIMIZE_INTERVAL", "3600")
    store = tmp_path / "g.omni"
    store.mkdir()

    def _boom(*a, **k):
        raise OSError("cannot spawn")

    monkeypatch.setattr(maintenance.subprocess, "Popen", _boom)

    # spawn fails but returns False (not raises); the stamp was still written so
    # the next stop within the window won't retry.
    assert maintenance.spawn_background_optimize(store, now=60_000.0) is False
    assert maintenance.due(store, now=60_000.0 + 5) is False


def test_mark_run_atomic_roundtrip(tmp_path, monkeypatch):
    from witan_code import maintenance

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    store = tmp_path / "g.omni"
    maintenance._mark_run(store, 12345.0)
    assert maintenance._last_run(store) == 12345.0
    # no leftover temp files from the atomic write
    assert not list(tmp_path.glob("*.tmp"))


# ── optimize/cleanup actually run against a real store ───────────────────────


def _fresh_store(tmp_path):
    from witan_code import config as cfg_module

    store = tmp_path / "graph.omni"
    subprocess.run(
        [
            "omnigraph",
            "init",
            "--schema",
            str(cfg_module.load().schema_file),
            str(store),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return store


@requires_omnigraph
def test_client_optimize_runs(tmp_path):
    from witan_code import config as cfg_module
    from witan_code.graph import OmnigraphClient

    store = _fresh_store(tmp_path)
    client = OmnigraphClient(str(store), cfg_module.load().queries_dir)
    # non-destructive, idempotent — just assert it completes without raising
    client.optimize()


@requires_omnigraph
def test_client_cleanup_requires_a_bound(tmp_path):
    import pytest

    from witan_code import config as cfg_module
    from witan_code.graph import OmnigraphClient

    store = _fresh_store(tmp_path)
    client = OmnigraphClient(str(store), cfg_module.load().queries_dir)
    with pytest.raises(ValueError):
        client.cleanup()
    # with a bound it runs
    client.cleanup(keep=5)


@requires_omnigraph
def test_cli_optimize_and_cleanup(tmp_path, capsys):
    from witan_code import cli as cli_module

    store = _fresh_store(tmp_path)

    cli_module.optimize(store=str(store))
    assert "Optimized" in capsys.readouterr().out

    # without --yes, cleanup refuses (destructive)
    cli_module.cleanup(store=str(store), keep=3)
    assert "--yes" in capsys.readouterr().out

    cli_module.cleanup(store=str(store), keep=3, yes=True)
    assert "Cleaned up" in capsys.readouterr().out


def test_cli_optimize_missing_store_is_noop(tmp_path, capsys):
    from witan_code import cli as cli_module

    cli_module.optimize(store=str(tmp_path / "does-not-exist.omni"))
    assert "nothing to do" in capsys.readouterr().out.lower()


def test_resolve_store_expands_user(tmp_path, monkeypatch):
    from witan_code import cli as cli_module

    # A `--store ~/…` path is expanded before the existence check, so an
    # existing store under HOME resolves instead of being treated as missing.
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "g.omni").mkdir()
    resolved = cli_module._resolve_store("~/g.omni")
    assert resolved == tmp_path / "g.omni"


def test_resolve_store_bridge_flag(tmp_path, monkeypatch):
    from witan_code import cli as cli_module
    from witan_code import config as cfg_module

    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path))
    bridge = tmp_path / cfg_module.BRIDGE_STORE_NAME
    bridge.mkdir()

    resolved = cli_module._resolve_store(None, bridge=True)
    assert resolved == bridge


def test_resolve_store_defaults_to_current_repo(tmp_path, monkeypatch):
    from witan_code import cli as cli_module
    from witan_code import config as cfg_module

    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/cg")
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path))
    store = cfg_module.store_path("https://github.com/test/cg", tmp_path)
    store.mkdir()

    assert cli_module._resolve_store(None) == store


@requires_omnigraph
def test_checkpoint_spawns_for_repo_and_bridge_stores(tmp_path, monkeypatch):
    from witan_code import cli as cli_module
    from witan_code import config as cfg_module
    from witan_code import maintenance as maintenance_module

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("WITAN_CODE_OPTIMIZE_INTERVAL", "3600")
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/cg")
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path))
    cfg_module.store_path("https://github.com/test/cg", tmp_path).mkdir(parents=True)
    cfg_module.bridge_store_path(tmp_path).mkdir(parents=True)

    calls = []
    monkeypatch.setattr(
        maintenance_module.subprocess,
        "Popen",
        lambda argv, **kw: calls.append(argv),
    )

    cli_module.checkpoint()

    assert len(calls) == 2
