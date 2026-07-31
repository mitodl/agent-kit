"""Tests for the global ``--output-format`` option."""

from __future__ import annotations

import datetime
import json
from types import SimpleNamespace

import pytest

from witan_code import cli as cli_module
from witan_code import output as output_module


@pytest.fixture(autouse=True)
def _reset_output_format():
    """Every test starts and ends on the txt default regardless of order."""
    output_module.set_output_format("txt")
    yield
    output_module.set_output_format("txt")


def test_render_table_json_dumps_normalized_rows(capsys):
    output_module.set_output_format("json")

    cli_module._render_table(
        title="Indexed repositories",
        columns=["repo", "files"],
        rows=[{"repo": "https://github.com/test/repo", "files": None}],
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "title": "Indexed repositories",
        "rows": [{"repo": "https://github.com/test/repo", "files": ""}],
    }


def test_render_table_toml_dumps_normalized_rows(capsys):
    import tomllib

    output_module.set_output_format("toml")

    cli_module._render_table(
        title="Symbol table — https://github.com/test/repo",
        columns=["role", "refs"],
        rows=[{"role": "exported", "refs": 2}],
    )

    payload = tomllib.loads(capsys.readouterr().out)
    assert payload["title"] == "Symbol table — https://github.com/test/repo"
    assert payload["rows"] == [{"role": "exported", "refs": 2}]


def test_repos_honors_structured_output(monkeypatch, capsys):
    # `repos` dispatches through _srv(), so stub the tool it calls: this test is
    # about rendering the rows, not about stores on disk.
    indexed = SimpleNamespace(
        code_indexed_repos=lambda: [
            {
                "repo": "https://github.com/test/repo",
                "files": 7,
                "bytes": 1024,
                "last_indexed": datetime.datetime(2026, 7, 13, 9, 41).timestamp(),
            }
        ]
    )
    monkeypatch.setattr(cli_module, "_srv", lambda: indexed)
    output_module.set_output_format("json")

    cli_module.repos()

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "title": "Indexed repositories",
        "rows": [
            {
                "repo": "https://github.com/test/repo",
                "files": "7",
                "size": "1.0KB",
                # Rendered from the epoch in the reader's local timezone.
                "last indexed": "2026-07-13 09:41",
            }
        ],
    }


def test_launcher_sets_output_format_and_forwards_tokens(monkeypatch):
    calls = []
    monkeypatch.setattr(cli_module, "app", lambda tokens: calls.append(tokens))

    cli_module._launcher("repos", output_format="yaml")

    assert output_module.get_output_format() == "yaml"
    assert calls == [("repos",)]
