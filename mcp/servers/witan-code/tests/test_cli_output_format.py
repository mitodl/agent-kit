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
        empty="",
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
        empty="",
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


# Each listing had its own early return that printed prose before the format
# was consulted, so each gets its own case rather than one standing in for all.
EMPTY_LISTINGS = {
    "symbols": (
        "code_repo_symbols",
        lambda: cli_module.symbols(repo="https://github.com/test/repo"),
    ),
    "stitch": ("code_precise_edges", lambda: cli_module.stitch()),
    "stitch-unresolved": (
        "code_unresolved_symbols",
        lambda: cli_module.stitch(unresolved=True),
    ),
    "repos": ("code_indexed_repos", lambda: cli_module.repos()),
    "branches": ("code_indexed_branches", lambda: cli_module.branches()),
}


@pytest.mark.parametrize(
    ("tool", "call"), EMPTY_LISTINGS.values(), ids=EMPTY_LISTINGS.keys()
)
@pytest.mark.parametrize("fmt", ["json", "yaml", "toml"])
def test_an_empty_listing_is_empty_rows_not_prose(monkeypatch, capsys, tool, call, fmt):
    import tomllib

    import yaml

    monkeypatch.setattr(
        cli_module, "_srv", lambda: SimpleNamespace(**{tool: lambda **_: []})
    )
    output_module.set_output_format(fmt)

    call()

    out = capsys.readouterr().out
    payload = {
        "json": json.loads,
        "yaml": yaml.safe_load,
        "toml": tomllib.loads,
    }[fmt](out)
    assert payload["rows"] == []


@pytest.mark.parametrize(
    ("tool", "call"), EMPTY_LISTINGS.values(), ids=EMPTY_LISTINGS.keys()
)
def test_an_empty_listing_still_says_so_in_txt(monkeypatch, capsys, tool, call):
    monkeypatch.setattr(
        cli_module, "_srv", lambda: SimpleNamespace(**{tool: lambda **_: []})
    )

    call()

    assert "No " in capsys.readouterr().out


def _stub_health(monkeypatch, *, ok: bool) -> None:
    from witan_code import server as server_module

    store = {
        "store": "/tmp/code/repo.omni",
        "label": "repo.omni",
        "kind": "repo",
        "ok": ok,
        "files": 3 if ok else None,
        "error": None if ok else "cannot open [repo.omni]",
        "stale_schema": False,
    }
    report = {"stores": [store], "ok": ok, "stale_schema": []}
    monkeypatch.setattr(server_module, "code_store_health", lambda: report)


def test_doctor_renders_in_txt(monkeypatch, capsys):
    # doctor reads through the local server module, not _srv(), so it had no
    # coverage from the listing stubs above, and a missing `empty=` crashed it.
    _stub_health(monkeypatch, ok=True)

    cli_module.doctor()

    out = capsys.readouterr().out
    assert "repo.omni" in out
    assert "Every code graph reads" in out


@pytest.mark.parametrize("ok", [True, False])
def test_doctor_structured_stdout_is_one_document(monkeypatch, capsys, ok):
    _stub_health(monkeypatch, ok=ok)
    output_module.set_output_format("json")

    if ok:
        cli_module.doctor()
    else:
        with pytest.raises(SystemExit):
            cli_module.doctor()

    captured = capsys.readouterr()
    assert json.loads(captured.out)["rows"][0]["store"] == "repo.omni"
    if not ok:
        assert "cannot be read" in captured.err


def test_a_bracketed_title_is_not_escaped_in_structured_output(capsys):
    output_module.set_output_format("json")

    cli_module._render_table(
        title="Symbol table — [local]",
        columns=["symbol"],
        rows=[{"symbol": "env:DATABASE_URL"}],
        empty="",
    )

    assert json.loads(capsys.readouterr().out)["title"] == "Symbol table — [local]"


def test_launcher_sets_output_format_and_forwards_tokens(monkeypatch):
    calls = []
    monkeypatch.setattr(cli_module, "app", lambda tokens: calls.append(tokens))

    cli_module._launcher("repos", output_format="yaml")

    assert output_module.get_output_format() == "yaml"
    assert calls == [("repos",)]
