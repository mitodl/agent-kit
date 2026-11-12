"""Error and notice text naming a long path keeps the path on one line.

With no standard stream a terminal and ``COLUMNS`` unset, Rich renders at 80
columns, and that is how an agent's shell or a CI job sees the CLI. A
hard-wrapped path longer than that is folded with a literal newline inside it,
so the path copied out of the error is not the path on disk. These consoles are
built the way the CLI's own are (no ``width``) after clearing the ``COLUMNS``
pin from ``testsupport/hermetic.py`` and hiding any terminal, so Rich picks the
width it picks for that case.
"""

from __future__ import annotations

import io
import os

import pytest

LONG_DIR = "a-directory-name-long-enough-to-push-the-whole-path-past-eighty-columns"


def _no_terminal(monkeypatch):
    """Make Rich see no terminal on any standard stream, with COLUMNS unset.

    Rich sizes a console from fds 0, 1 and 2 before falling back to 80, so
    clearing COLUMNS alone still picks up a terminal under ``pytest -s``.
    """

    def _not_a_terminal(*_args):
        raise OSError

    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setattr(os, "get_terminal_size", _not_a_terminal)


@pytest.fixture
def piped(monkeypatch):
    """Swap witan's stdout and stderr consoles for non-terminal ones."""
    from rich.console import Console

    from witan.cli import _common

    _no_terminal(monkeypatch)
    out = Console(file=io.StringIO())
    err = Console(file=io.StringIO())
    assert not out.is_terminal
    assert out.width == 80
    monkeypatch.setattr(_common, "console", out)
    monkeypatch.setattr(_common, "stderr_console", err)
    monkeypatch.setattr("witan.cli.targets.console", out)
    monkeypatch.setattr("witan.cli.local_dispatch.stderr_console", err)
    return out, err


def _long_path(tmp_path):
    path = tmp_path / LONG_DIR / "config.toml"
    assert len(str(path)) > 80
    return path


def _on_one_line(text: str, needle: str) -> bool:
    return any(needle in line for line in text.splitlines())


@pytest.mark.parametrize("stderr", [False, True])
def test_print_error_keeps_a_long_path_on_one_line(piped, tmp_path, stderr):
    from witan.cli._common import print_error

    path = _long_path(tmp_path)
    print_error(f"{path} already exists (use --force to overwrite)", stderr=stderr)

    text = piped[stderr].file.getvalue()
    assert _on_one_line(text, str(path)), text


def test_print_notice_keeps_a_long_path_on_one_line(piped, tmp_path):
    from witan.cli._common import print_notice

    path = _long_path(tmp_path)
    print_notice(f"[yellow]No targets configured[/yellow] in {path}.")

    assert _on_one_line(piped[0].file.getvalue(), str(path))


def test_target_add_refusal_keeps_the_config_path_on_one_line(
    piped, tmp_path, monkeypatch
):
    from witan.cli.targets import add

    path = _long_path(tmp_path)
    path.parent.mkdir()
    path.write_text("[targets.hosted]\nremote_url = 'https://old.example/mcp'\n")
    monkeypatch.setenv("WITAN_CONFIG", str(path))
    monkeypatch.delenv("WITAN_TARGET", raising=False)

    with pytest.raises(SystemExit):
        add("hosted", server="/srv/witan.omni")

    text = piped[0].file.getvalue()
    assert "already exists" in text
    assert _on_one_line(text, str(path)), text


def test_a_table_still_wraps_to_the_width(piped):
    # Soft wrap is for prose. A table that ran past the width would push its
    # columns out of alignment, so it keeps Rich's own wrapping.
    from witan.cli._common import render_table

    render_table(
        title="Tasks",
        columns=["slug", "title"],
        rows=[{"slug": "tk-x", "title": "word " * 40}],
        empty="",
    )

    lines = piped[0].file.getvalue().splitlines()
    assert len(lines) > 4
    assert max(len(line) for line in lines) <= 80


def test_local_store_notice_keeps_the_target_name_and_the_path(piped, tmp_path):
    # The notice is markup, and `target [qa]` inside it reads to Rich as a
    # style tag, which dropped the name of the target that matched.
    from witan.cli import local_dispatch
    from witan.config import LocalDispatch

    path = _long_path(tmp_path).with_suffix(".omni")
    diagnosis = LocalDispatch(
        graph_uri=str(path),
        target_name="qa",
        deployed_targets=("prod",),
        deliberate=True,
    )
    local_dispatch.local_server(diagnosis, load_inner=lambda: None)

    text = piped[1].file.getvalue()
    assert "target [qa]" in text
    assert _on_one_line(text, str(path)), text
