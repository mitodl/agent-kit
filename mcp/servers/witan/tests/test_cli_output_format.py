"""Tests for the global ``--output-format`` option and ``render_table``."""

from __future__ import annotations

import json

import pytest

from witan.cli import output as output_module
from witan.cli._common import render_table


@pytest.fixture(autouse=True)
def _reset_output_format():
    """Every test starts and ends on the txt default regardless of order."""
    output_module.set_output_format("txt")
    yield
    output_module.set_output_format("txt")


def _rows():
    return [
        {"slug": "tk-a", "status": "open", "title": "First"},
        {"slug": "tk-b", "status": "closed", "title": "Second"},
    ]


def test_render_table_txt_prints_rich_table(monkeypatch):
    from witan.cli import _common

    captured = []
    monkeypatch.setattr(
        _common.console, "print", lambda *a, **kw: captured.append(a[0])
    )

    render_table(title="Tasks", columns=["slug", "status", "title"], rows=_rows())

    assert len(captured) == 1
    table = captured[0]
    assert hasattr(table, "columns")
    assert table.title == "Tasks"


def test_render_table_json_dumps_raw_rows(capsys):
    output_module.set_output_format("json")

    render_table(title="Tasks", columns=["slug", "status", "title"], rows=_rows())

    payload = json.loads(capsys.readouterr().out)
    assert payload["title"] == "Tasks"
    assert payload["rows"] == _rows()


def test_render_table_yaml_dumps_raw_rows(capsys):
    import yaml

    output_module.set_output_format("yaml")

    render_table(title="Tasks", columns=["slug", "status", "title"], rows=_rows())

    payload = yaml.safe_load(capsys.readouterr().out)
    assert payload["title"] == "Tasks"
    assert payload["rows"] == _rows()


def test_render_table_toml_dumps_raw_rows(capsys):
    import tomllib

    output_module.set_output_format("toml")

    render_table(title="Tasks", columns=["slug", "status", "title"], rows=_rows())

    payload = tomllib.loads(capsys.readouterr().out)
    assert payload["title"] == "Tasks"
    assert payload["rows"] == _rows()


def test_render_table_structured_formats_ignore_styling(capsys):
    """Styling/placeholders are txt-only presentation, never leak into structured dumps."""
    output_module.set_output_format("json")

    render_table(
        title="Tasks",
        columns=["slug", "status"],
        rows=[{"slug": "tk-a", "status": ""}],
        styles={"status": {"open": "green"}},
        placeholders={"status": "(none)"},
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"] == [{"slug": "tk-a", "status": ""}]


def test_launcher_sets_output_format_and_forwards_tokens(monkeypatch):
    import witan.cli as cli_module

    calls = []
    monkeypatch.setattr(cli_module, "app", lambda tokens: calls.append(tokens))

    cli_module._launcher("tasks", "--all-repos", output_format="yaml")

    assert output_module.get_output_format() == "yaml"
    assert calls == [("tasks", "--all-repos")]


def test_launcher_defaults_to_txt(monkeypatch):
    import witan.cli as cli_module

    monkeypatch.setattr(cli_module, "app", lambda tokens: None)

    cli_module._launcher("tasks")

    assert output_module.get_output_format() == "txt"
