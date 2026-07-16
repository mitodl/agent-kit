from witan_core import maintenance


def test_resolve_interval_env_override(monkeypatch):
    monkeypatch.delenv("X_INTERVAL", raising=False)
    assert (
        maintenance.resolve_interval("X_INTERVAL")
        == maintenance.DEFAULT_OPTIMIZE_INTERVAL
    )
    monkeypatch.setenv("X_INTERVAL", "3600")
    assert maintenance.resolve_interval("X_INTERVAL") == 3600.0
    monkeypatch.setenv("X_INTERVAL", "0")
    assert maintenance.resolve_interval("X_INTERVAL") == 0.0
    # non-numeric falls back to the default
    monkeypatch.setenv("X_INTERVAL", "nope")
    assert maintenance.resolve_interval("X_INTERVAL", 42.0) == 42.0


def test_mark_run_last_run_atomic_roundtrip(tmp_path):
    stamp = tmp_path / "stamp.json"
    maintenance.mark_run(stamp, 12345.0)
    assert maintenance.last_run(stamp) == 12345.0
    # no leftover temp files
    assert not list(tmp_path.glob("*.tmp"))


def test_last_run_missing_or_corrupt_is_zero(tmp_path):
    assert maintenance.last_run(tmp_path / "absent.json") == 0.0
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{ not json")
    assert maintenance.last_run(corrupt) == 0.0


def test_is_due_branches(tmp_path):
    store = tmp_path / "graph.omni"
    store.mkdir()
    stamp = tmp_path / "stamp.json"
    kw = dict(store=str(store), stamp_file=stamp, now=10_000.0)

    # disabled
    assert maintenance.is_due(interval=0, require_exists=False, **kw) is False
    # remote store never fires
    assert (
        maintenance.is_due(
            store="https://example.com/g",
            stamp_file=stamp,
            interval=3600,
            now=10_000.0,
            require_exists=False,
        )
        is False
    )
    # missing store when require_exists
    assert (
        maintenance.is_due(
            store=str(tmp_path / "absent.omni"),
            stamp_file=stamp,
            interval=3600,
            now=10_000.0,
            require_exists=True,
        )
        is False
    )
    # due when never run
    assert maintenance.is_due(interval=3600, require_exists=True, **kw) is True
    # not due right after a run; due again after the window
    maintenance.mark_run(stamp, 10_000.0)
    assert (
        maintenance.is_due(
            interval=3600,
            require_exists=True,
            store=str(store),
            stamp_file=stamp,
            now=10_100.0,
        )
        is False
    )
    assert (
        maintenance.is_due(
            interval=3600,
            require_exists=True,
            store=str(store),
            stamp_file=stamp,
            now=14_000.0,
        )
        is True
    )
