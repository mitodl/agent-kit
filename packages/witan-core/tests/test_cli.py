"""CLI scaffolding shared by both servers' ``setup`` commands.

The two servers rendered install results differently (rich markup vs plain
print) and each carried its own copy of the agent-name constants and author
resolution; these tests pin the shared behavior so a future divergence is caught
here rather than in one server only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from witan_core.cli import (
    AGENT_NAMES,
    AgentName,
    make_app,
    report_install,
    resolve_author,
)


def _result(planned, skipped):
    # report_install only reads .planned and .skipped, so a stub stands in for
    # agent_config_kit.InstallResult (keeps the base test deps light).
    return SimpleNamespace(planned=planned, skipped=skipped)


def test_agent_names_cover_the_literal():
    # Every AgentName except the "all" fan-out has a display label.
    literals = set(AgentName.__args__) - {"all"}
    assert literals == set(AGENT_NAMES)


def test_resolve_author_prefers_explicit():
    assert resolve_author("Ada Lovelace") == "Ada Lovelace"


def test_resolve_author_falls_back_to_git(monkeypatch):
    monkeypatch.setattr("subprocess.check_output", lambda *a, **k: "Git Name\n")
    assert resolve_author(None) == "Git Name"


def test_resolve_author_falls_back_to_user_env(monkeypatch):
    def _no_git(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr("subprocess.check_output", _no_git)
    monkeypatch.setenv("USER", "env_user")
    assert resolve_author(None) == "env_user"


def test_resolve_author_last_resort_unknown(monkeypatch):
    monkeypatch.setattr("subprocess.check_output", lambda *a, **k: "  \n")
    monkeypatch.delenv("USER", raising=False)
    assert resolve_author(None) == "unknown"


def test_report_install_plain(capsys):
    report_install(
        "claude",
        _result(planned=["a.json"], skipped=[("b.json", "exists")]),
        dry_run=False,
    )
    out = capsys.readouterr().out
    assert "Claude Code" in out
    assert "-> a.json" in out
    assert "skip b.json — exists" in out
    assert "[green]" not in out  # plain branch: no rich markup


def test_report_install_dry_run_tag(capsys):
    report_install("pi", _result(planned=["x"], skipped=[]), dry_run=True)
    assert "(dry-run)" in capsys.readouterr().out


def test_report_install_rich_uses_console_markup():
    calls: list[str] = []
    console = SimpleNamespace(print=lambda s: calls.append(s))
    report_install(
        "claude",
        _result(planned=["a.json"], skipped=[("b.json", "exists")]),
        dry_run=False,
        console=console,
    )
    joined = "\n".join(calls)
    assert "[bold]Claude Code[/bold]" in joined
    assert "[green]→[/green] a.json" in joined
    assert "[yellow]skip[/yellow] b.json — exists" in joined


def test_make_app_sets_version(capsys):
    app = make_app(
        name="witan-core-test",
        help_text="test app",
        version_dist="witan-core",
    )
    with pytest.raises(SystemExit):
        app(["--version"])
    # resolve_version returns *some* string for the installed distribution.
    assert capsys.readouterr().out.strip()
