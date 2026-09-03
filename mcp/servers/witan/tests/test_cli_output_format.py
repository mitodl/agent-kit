"""Tests for the global ``--output-format`` option and ``render_table``."""

from __future__ import annotations

import json
import sys
import types

import pytest

from witan.cli import output as output_module
from witan.cli._common import render_table

# Imported from the module, not `witan.cli`: the package re-exports
# `selected_target` as a FUNCTION, which shadows the submodule name.
from witan.cli.selected_target import set_selected_target


@pytest.fixture(autouse=True)
def _reset_output_format():
    """Every test starts and ends on the txt default regardless of order."""
    output_module.set_output_format("txt")
    yield
    output_module.set_output_format("txt")


@pytest.fixture(autouse=True)
def _reset_selected_target():
    """Same, for the launcher's other piece of module-level state.

    Any test that drives `_launcher` with a `target` sets it process-wide, and
    without this the value leaks into every test that runs afterwards — which
    is how one test here took out nine elsewhere, all of them asserting on a
    target they never set.
    """
    set_selected_target(None)
    yield
    set_selected_target(None)


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


def test_launcher_propagates_output_format_to_mounted_witan_code(monkeypatch):
    import witan.cli as cli_module

    calls = []
    fake_pkg = types.ModuleType("witan_code")
    fake_output = types.ModuleType("witan_code.output")
    fake_output.set_output_format = calls.append
    fake_target = types.ModuleType("witan_code.selected_target")
    fake_target.set_selected_target = lambda name: None
    monkeypatch.setitem(sys.modules, "witan_code", fake_pkg)
    monkeypatch.setitem(sys.modules, "witan_code.output", fake_output)
    monkeypatch.setitem(sys.modules, "witan_code.selected_target", fake_target)
    monkeypatch.setattr(cli_module, "app", lambda tokens: None)

    cli_module._launcher("code", "repos", output_format="json")

    assert calls == ["json"]


def test_a_witan_code_too_old_for_target_keeps_output_format(monkeypatch):
    """A stale witan-code must cost only the forwarding it is too old for.

    `witan_code.selected_target` exists from witan-code 0.18.0; `witan_code.
    output` has been there for many releases. witan-code is not a dependency of
    this package — it is an optional runtime mount installed alongside — so
    nothing constrains the pair and an upgrade of one without the other is an
    ordinary user state.

    Both forwardings used to share one `try`, so this arrangement sent BOTH down
    `except ImportError`: upgrading witan-council alone silently disabled
    `--output-format` for `witan code …`, with no error. Raised on the 0.31.0
    release PR (#319).
    """
    import witan.cli as cli_module

    calls = []
    fake_pkg = types.ModuleType("witan_code")
    fake_output = types.ModuleType("witan_code.output")
    fake_output.set_output_format = calls.append
    monkeypatch.setitem(sys.modules, "witan_code", fake_pkg)
    monkeypatch.setitem(sys.modules, "witan_code.output", fake_output)
    # The stale half: absent, the way it is on witan-code < 0.18.0. Set to None
    # rather than left alone, so a real installed witan-code in the test env
    # cannot satisfy the import and hide the regression.
    monkeypatch.setitem(sys.modules, "witan_code.selected_target", None)
    monkeypatch.setattr(cli_module, "app", lambda tokens: None)

    cli_module._launcher("code", "repos", output_format="json", target="qa")

    assert calls == ["json"]
