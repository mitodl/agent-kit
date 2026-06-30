"""Tests for `witan setup`'s CLI-bootstrap step (`_ensure_witan_cli`).

The installed hooks call a bare `witan` command, so setup must make sure the
CLI is on PATH. These tests pin that bootstrap without invoking uv for real.
"""

from witan.cli import setup_cmd


def _patch_which(monkeypatch, present):
    """Stub shutil.which: a command resolves iff it's in `present`."""
    monkeypatch.setattr(
        setup_cmd.shutil,
        "which",
        lambda cmd: f"/usr/bin/{cmd}" if cmd in present else None,
    )


def _capture_run(monkeypatch):
    """Record subprocess.run calls and return the list of argv lists."""
    calls = []
    monkeypatch.setattr(
        setup_cmd.subprocess, "run", lambda argv, **kw: calls.append(argv)
    )
    return calls


def test_installs_witan_cli_when_absent_and_uv_present(monkeypatch):
    """With witan off PATH and uv available, setup runs `uv tool install`."""
    _patch_which(monkeypatch, present={"uv"})
    calls = _capture_run(monkeypatch)

    setup_cmd._ensure_witan_cli(dry_run=False)

    assert calls == [["uv", "tool", "install", "--quiet", setup_cmd._WITAN_PKG]]


def test_skips_install_when_witan_already_on_path(monkeypatch):
    """If witan already resolves, setup does not shell out at all."""
    _patch_which(monkeypatch, present={"witan", "uv"})
    calls = _capture_run(monkeypatch)

    setup_cmd._ensure_witan_cli(dry_run=False)

    assert calls == []


def test_dry_run_does_not_install(monkeypatch):
    """--dry-run reports intent without running uv."""
    _patch_which(monkeypatch, present={"uv"})
    calls = _capture_run(monkeypatch)

    setup_cmd._ensure_witan_cli(dry_run=True)

    assert calls == []


def test_no_install_attempt_when_uv_missing(monkeypatch):
    """Without uv there's nothing to install with — fall back to the warning."""
    _patch_which(monkeypatch, present=set())
    calls = _capture_run(monkeypatch)

    setup_cmd._ensure_witan_cli(dry_run=False)

    assert calls == []
