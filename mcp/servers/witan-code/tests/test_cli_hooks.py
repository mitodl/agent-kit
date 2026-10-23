"""Unit tests for the thin CLI wrappers around the hook backends.

These wrap context.inject_context()/hooks.*() and must never let an
exception escape (the hooks are documented as "always exits 0"), and must
not print an extra trailing newline beyond what the backend already returns.
"""

import io
import json
import sys

import pytest

from witan_code import cli


def test_inject_context_cmd_prints_backend_output_without_extra_newline(
    monkeypatch, capsys
):
    import witan_code.context as context_module

    monkeypatch.setattr(
        context_module, "inject_context", lambda client: "## Code Graph\n\ntext.\n"
    )

    cli.inject_context_cmd()

    assert capsys.readouterr().out == "## Code Graph\n\ntext.\n"


def test_inject_context_cmd_prints_nothing_for_empty_backend_output(
    monkeypatch, capsys
):
    import witan_code.context as context_module

    monkeypatch.setattr(context_module, "inject_context", lambda client: "")

    cli.inject_context_cmd()

    assert capsys.readouterr().out == ""


def test_inject_context_cmd_never_raises_when_backend_fails(monkeypatch, capsys):
    import witan_code.context as context_module

    def _boom(client):
        raise RuntimeError("graph read failed")

    monkeypatch.setattr(context_module, "inject_context", _boom)

    cli.inject_context_cmd()  # must not raise

    assert capsys.readouterr().out == ""


def test_inject_context_cmd_defaults_to_the_claude_client(monkeypatch, capsys):
    """The Claude hook runs the bare command, so the default must stay Claude."""
    import witan_code.context as context_module

    seen = []
    monkeypatch.setattr(
        context_module, "inject_context", lambda client: seen.append(client) or ""
    )

    command, bound, _ = cli.app.parse_args(["inject-context"])
    command(*bound.args, **bound.kwargs)

    assert seen == ["claude"]


def _stdin(monkeypatch, payload: dict) -> None:
    """Hand the command a hook payload on a closed, non-tty stdin."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))


def _run_bare(monkeypatch) -> list[str]:
    import witan_code.context as context_module

    seen = []
    monkeypatch.setattr(
        context_module, "inject_context", lambda client: seen.append(client) or ""
    )
    command, bound, _ = cli.app.parse_args(["inject-context"])
    command(*bound.args, **bound.kwargs)
    return seen


def test_the_bare_command_renders_for_codex_from_a_codex_payload(monkeypatch):
    """One hook command is registered for Claude Code and Codex alike, so the
    payload is what picks the block."""
    from .conftest import CODEX_PROMPT_PAYLOAD

    _stdin(monkeypatch, CODEX_PROMPT_PAYLOAD)

    assert _run_bare(monkeypatch) == ["codex"]


def test_the_bare_command_stays_claude_for_a_claude_payload(monkeypatch):
    from .conftest import CLAUDE_PROMPT_PAYLOAD

    _stdin(monkeypatch, CLAUDE_PROMPT_PAYLOAD)

    assert _run_bare(monkeypatch) == ["claude"]


def test_an_explicit_client_does_not_read_stdin(monkeypatch):
    """The Pi extension passes `--client`; its stdin is not a hook payload."""
    import witan_code.context as context_module

    from .conftest import CODEX_PROMPT_PAYLOAD

    _stdin(monkeypatch, CODEX_PROMPT_PAYLOAD)
    seen = []
    monkeypatch.setattr(
        context_module, "inject_context", lambda client: seen.append(client) or ""
    )

    command, bound, _ = cli.app.parse_args(["inject-context", "--client", "pi"])
    command(*bound.args, **bound.kwargs)

    assert seen == ["pi"]
    assert sys.stdin.read() != ""  # left unread


@pytest.mark.parametrize("client", ["codex", "pi", "pi-builtin"])
def test_inject_context_cmd_passes_an_explicit_client_through(monkeypatch, client):
    import witan_code.context as context_module

    seen = []
    monkeypatch.setattr(
        context_module, "inject_context", lambda client: seen.append(client) or ""
    )

    command, bound, _ = cli.app.parse_args(["inject-context", "--client", client])
    command(*bound.args, **bound.kwargs)

    assert seen == [client]
