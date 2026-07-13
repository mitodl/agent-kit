"""Tests for the global ``--output-format`` option."""

from __future__ import annotations

import json

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


def test_repos_honors_structured_output(tmp_path, monkeypatch, capsys):
    code_dir = tmp_path / "code"
    code_dir.mkdir()
    store = code_dir / "https_github.com_test_repo.omni"
    store.mkdir()
    (code_dir / f"{store.name}.repo").write_text("https://github.com/test/repo")
    monkeypatch.setenv("WITAN_CODE_DIR", str(code_dir))
    monkeypatch.setattr(
        cli_module,
        "_code_store_stats",
        lambda store: ("https://github.com/test/repo", "7"),
    )
    monkeypatch.setattr(cli_module, "_dir_stats", lambda path: (1024, "2026-07-13"))
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
                "last indexed": "2026-07-13",
            }
        ],
    }


def test_launcher_sets_output_format_and_forwards_tokens(monkeypatch):
    calls = []
    monkeypatch.setattr(cli_module, "app", lambda tokens: calls.append(tokens))

    cli_module._launcher("repos", output_format="yaml")

    assert output_module.get_output_format() == "yaml"
    assert calls == [("repos",)]
