"""Tests for `witan scan test` / `witan scan rules` (dry-run + introspection)."""

from __future__ import annotations


def _capture(monkeypatch):
    """Swap the CLI's console for a recording one and return its rendered text.

    Table output only round-trips through Rich's own renderer, so a plain
    ``str(arg)`` capture (fine for the plain strings other CLI tests print)
    would just show a Table object repr.
    """
    from rich.console import Console

    from witan.cli import _common

    recorder = Console(record=True, width=200)
    monkeypatch.setattr(_common, "console", recorder)
    monkeypatch.setattr("witan.cli.scan.console", recorder)
    return recorder


def _isolate_config(monkeypatch, tmp_path):
    """Point WITAN_CONFIG at a nonexistent file so a real ~/.config isn't read."""
    monkeypatch.setenv("WITAN_CONFIG", str(tmp_path / "no-such-config.toml"))
    for var in (
        "WITAN_SCAN_ENABLED",
        "WITAN_SCAN_SECRET_ACTION",
        "WITAN_SCAN_PII_ACTION",
        "WITAN_SCAN_ENABLED_DETECTORS",
        "WITAN_SCAN_DISABLED_DETECTORS",
        "WITAN_SCAN_PLUGINS",
    ):
        monkeypatch.delenv(var, raising=False)


def test_rules_lists_builtin_detectors(monkeypatch, tmp_path):
    from witan.cli.scan import rules

    _isolate_config(monkeypatch, tmp_path)
    recorder = _capture(monkeypatch)
    rules()

    combined = recorder.export_text()
    assert "aws_access_key" in combined
    assert "built-in" in combined
    assert "enabled" in combined  # scanning on by default (opt-out)


def test_rules_reports_disabled_when_opted_out(monkeypatch, tmp_path):
    from witan.cli.scan import rules

    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setenv("WITAN_SCAN_ENABLED", "false")
    recorder = _capture(monkeypatch)
    rules()

    assert "disabled" in recorder.export_text()


def test_rules_respects_disabled_detectors(monkeypatch, tmp_path):
    from witan.cli.scan import rules

    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setenv("WITAN_SCAN_DISABLED_DETECTORS", "email")
    recorder = _capture(monkeypatch)
    rules()

    combined = recorder.export_text()
    assert "aws_access_key" in combined
    assert "email" not in combined


def test_test_command_reports_no_findings_for_clean_text(monkeypatch, tmp_path):
    from witan.cli.scan import test as scan_test

    _isolate_config(monkeypatch, tmp_path)
    recorder = _capture(monkeypatch)
    scan_test("nothing interesting here")

    combined = recorder.export_text()
    assert "No findings" in combined


def test_test_command_reports_findings_without_leaking_the_value(monkeypatch, tmp_path):
    from witan.cli.scan import test as scan_test

    _isolate_config(monkeypatch, tmp_path)
    recorder = _capture(monkeypatch)
    fake_key = "AKIAIOSFODNN7EXAMPLE"  # pragma: allowlist secret gitleaks:allow
    scan_test(f"key {fake_key} here")

    combined = recorder.export_text()
    assert "aws_access_key" in combined
    assert "would block" in combined
    assert fake_key not in combined


def test_test_command_notes_when_scanning_disabled(monkeypatch, tmp_path):
    from witan.cli.scan import test as scan_test

    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setenv("WITAN_SCAN_ENABLED", "false")
    recorder = _capture(monkeypatch)
    scan_test("clean text")

    combined = recorder.export_text()
    assert "disabled" in combined


def test_test_command_silent_when_scanning_enabled(monkeypatch, tmp_path):
    """Enabled by default (opt-out) — no disabled-note noise in the common case."""
    from witan.cli.scan import test as scan_test

    _isolate_config(monkeypatch, tmp_path)
    recorder = _capture(monkeypatch)
    scan_test("clean text")

    assert "disabled" not in recorder.export_text()
