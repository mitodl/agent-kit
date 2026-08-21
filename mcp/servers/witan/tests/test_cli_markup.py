"""Stored text reaches the terminal whole, brackets included.

Rich reads ``[...]`` in ``Console.print`` as a style tag, so any stored value
holding a TOML section, a Python repr, or a markdown link renders with that
substring silently removed — and nothing says it was. It was found on a task
resolution that named the misconfigured target: ``[targets.production]`` was
gone from the printed sentence while ``task_get`` showed it stored intact.

agent-kit#261 fixed the two ``witan serve`` call sites it had just written and
every other renderer stayed broken, so these tests assert the escaping at the
two shared boundaries (``esc``/``print_error`` and ``render_table``) and then
through a command that prints stored content. They render through a real
console: markup is eaten at render time, so a test that captures ``print``'s
arguments passes while the user sees the hole.
"""

from __future__ import annotations

import pytest

# The exact string from the report: a TOML table header, which Rich reads as a
# style named `targets.production` and drops when it cannot resolve it.
BRACKETED = (
    "code_transport is not set on [targets.production], so code graphs stay local"
)


@pytest.fixture
def render():
    """Run a CLI renderer and return what a terminal would actually show."""
    from witan.cli._common import console

    console.width = 200  # wide, so wrapping never splits a string under test

    def _render(fn, *args, **kwargs):
        with console.capture() as capture:
            fn(*args, **kwargs)
        return capture.get()

    return _render


def test_esc_keeps_a_toml_section_in_the_rendered_line(render):
    from witan.cli._common import console, esc

    out = render(console.print, f"resolution: {esc(BRACKETED)}")

    assert "[targets.production]" in out


def test_an_error_naming_the_config_block_to_fix_still_names_it(render):
    # Error text is the worst place to lose brackets: witan's own refusals name
    # the block to edit, and that name is the whole point of the sentence.
    from witan.cli._common import print_error

    out = render(print_error, ValueError("unset `remote_url` on target [qa]"))

    assert "[qa]" in out


def test_a_table_cell_keeps_its_brackets(render):
    from witan.cli._common import render_table

    out = render(
        render_table,
        title="Tasks",
        columns=["slug", "title"],
        rows=[{"slug": "tk-x", "title": "fix [targets.production]"}],
    )

    assert "[targets.production]" in out


def test_styling_a_column_still_works_after_escaping(render):
    # The escape must not swallow the styles the renderer itself applies —
    # those are markup we wrote, not data we were handed.
    from witan.cli._common import _STATUS_STYLE, render_table

    out = render(
        render_table,
        title="Tasks",
        columns=["status", "title"],
        rows=[{"status": "blocked", "title": "x"}],
        styles={"status": _STATUS_STYLE},
    )

    assert "blocked" in out
    assert "[red]" not in out  # consumed as a style, not printed as text


def _stub_server(**tools):
    """A server exposing only the named tools, and no ``client``."""

    class _Stub:
        def __getattr__(self, name):
            if name in tools:
                return tools[name]
            raise AssertionError(f"unexpected attribute: {name}")

    return _Stub()


def test_task_show_prints_a_resolution_containing_a_toml_section(render, monkeypatch):
    """The originally observed defect, end to end."""
    from witan.cli import _common
    from witan.cli.tasks import _task_show

    task = {
        "slug": "tk-x",
        "title": "witan serve falls back to the local store",
        "type": "bug",
        "priority": "p2",
        "status": "closed",
        "description": f"Seen when {BRACKETED}",
        "resolution": BRACKETED,
    }
    monkeypatch.setattr(
        _common,
        "_server",
        _stub_server(task_get=lambda slug: task, task_list=lambda parent: []),
    )

    out = render(_task_show, "tk-x")

    assert out.count("[targets.production]") == 2  # description and resolution


def test_a_bracketed_path_does_not_take_the_command_down(render, monkeypatch):
    # The one shape that fails loudly instead of silently: `[/var/lib/witan]`
    # parses as a CLOSING tag with nothing open, which raises MarkupError —
    # so an un-escaped store path in a description kills the whole command
    # rather than losing its own substring.
    from witan.cli import _common
    from witan.cli.tasks import _task_show

    task = {
        "slug": "tk-y",
        "title": "recall() drops rows where [rank] is null",
        "status": "open",
        "priority": "p1",
        "type": "bug",
        "description": "the store at [/var/lib/witan/graph.omni] is stale",
    }
    monkeypatch.setattr(
        _common,
        "_server",
        _stub_server(task_get=lambda slug: task, task_list=lambda parent: []),
    )

    out = render(_task_show, "tk-y")

    assert "[rank]" in out
    assert "[/var/lib/witan/graph.omni]" in out


def test_a_dry_run_prompt_is_shown_exactly_as_the_agent_will_receive_it(render):
    """The one place escaping is the wrong answer, and `markup=False` is half of it.

    A dry run exists to show the text that will be handed to the agent, so
    escapes must not appear in it — but Rich substitutes emoji codes and
    highlights literals independently of markup, so `:warning:` in a task
    description rendered as ⚠ while the agent received the eight original
    characters. Same class of lie, different Rich feature.
    """
    from witan.cli.run_helpers import _launch_agent

    prompt = "Fix the :warning: banner in [targets.production] before 3.14"
    out = render(_launch_agent, None, "claude", None, prompt, True)

    assert out.strip() == prompt
