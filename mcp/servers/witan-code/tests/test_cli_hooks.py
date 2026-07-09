"""Unit tests for the thin CLI wrappers around the hook backends.

These wrap context.inject_context()/hooks.*() and must never let an
exception escape (the hooks are documented as "always exits 0"), and must
not print an extra trailing newline beyond what the backend already returns.
"""

from witan_code import cli


def test_inject_context_cmd_prints_backend_output_without_extra_newline(
    monkeypatch, capsys
):
    import witan_code.context as context_module

    monkeypatch.setattr(
        context_module, "inject_context", lambda: "## Code Graph\n\ntext.\n"
    )

    cli.inject_context_cmd()

    assert capsys.readouterr().out == "## Code Graph\n\ntext.\n"


def test_inject_context_cmd_prints_nothing_for_empty_backend_output(
    monkeypatch, capsys
):
    import witan_code.context as context_module

    monkeypatch.setattr(context_module, "inject_context", lambda: "")

    cli.inject_context_cmd()

    assert capsys.readouterr().out == ""


def test_inject_context_cmd_never_raises_when_backend_fails(monkeypatch, capsys):
    import witan_code.context as context_module

    def _boom():
        raise RuntimeError("graph read failed")

    monkeypatch.setattr(context_module, "inject_context", _boom)

    cli.inject_context_cmd()  # must not raise

    assert capsys.readouterr().out == ""
