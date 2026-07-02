import importlib
import sys

import pytest


def test_cli_help_smoke(capsys):
    from agent_config_kit.cli import app

    with pytest.raises(SystemExit) as exc_info:
        app(["--help"])

    assert exc_info.value.code == 0
    assert "ac-kit" in capsys.readouterr().out.lower()


def test_cli_without_extra_exits_with_friendly_message(monkeypatch, capsys):
    """Importing agent_config_kit.cli without cyclopts installed must fail
    fast with an actionable message, not a bare traceback."""
    monkeypatch.setitem(sys.modules, "cyclopts", None)
    sys.modules.pop("agent_config_kit.cli", None)

    try:
        with pytest.raises(SystemExit) as exc_info:
            importlib.import_module("agent_config_kit.cli")
        assert exc_info.value.code == 1
        assert "cli" in capsys.readouterr().err.lower()
    finally:
        sys.modules.pop("agent_config_kit.cli", None)
        monkeypatch.delitem(sys.modules, "cyclopts", raising=False)
        importlib.import_module("agent_config_kit.cli")
