"""The terminal as an elicitation surface.

A CLI dispatches tools in-process, bypassing MCP, so FastMCP injects no
``Context`` and every ``confirm``/``text`` took its ``ctx is None`` branch — the
prompts were unreachable *by construction* for a local CLI user, while the
remote CLI path has always reached them through the proxy's
``console_elicitation_handler``. ``ConsoleContext`` closes that gap, and
``with_console_ctx`` is the seam a CLI's tool-unwrapper calls.

``console_prompt`` itself is now shared with that remote handler, so the two
dispatch paths cannot drift on what a blank answer or a missing tty means —
``test_remote_proxy.py`` covers the wire adapter around it.
"""

import asyncio

import pytest

from witan_core import elicit


class _Tty:
    def isatty(self):
        return True


def _typed(monkeypatch, answer, *, tty=True):
    monkeypatch.setattr("sys.stdin", _Tty() if tty else None)
    if isinstance(answer, type) and issubclass(answer, BaseException):

        def _input(_prompt):
            raise answer

    else:

        def _input(_prompt):
            return answer

    monkeypatch.setattr("builtins.input", _input)


def _confirm(default=False):
    return asyncio.run(
        elicit.confirm(
            elicit.ConsoleContext(),
            "Steal the claim?",
            default_when_unsupported=default,
            title="Steal claim?",
        )
    )


def _text():
    return asyncio.run(
        elicit.text(
            elicit.ConsoleContext(), "Name it:", default="fallback", title="Name"
        )
    )


def test_confirm_reaches_the_terminal_and_honours_both_answers(monkeypatch):
    _typed(monkeypatch, "y")
    assert _confirm() is True
    _typed(monkeypatch, "YES")
    assert _confirm() is True
    # A typed "n" is a real answer, not a decline — matching the remote path.
    _typed(monkeypatch, "n")
    assert _confirm() is False


@pytest.mark.parametrize("answer", ["", "   ", EOFError, KeyboardInterrupt])
def test_a_refusal_at_the_prompt_is_false_whatever_the_default(monkeypatch, answer):
    """Blank at ``[y/N]``, Ctrl-D and Ctrl-C are refusals by a human who saw the
    question. They must NOT be read as ``default_when_unsupported``: Ctrl-C at
    "Proceed?" never means yes, whatever the non-interactive default is."""
    _typed(monkeypatch, answer)
    assert _confirm(default=False) is False
    assert _confirm(default=True) is False


def test_text_reaches_the_terminal(monkeypatch):
    _typed(monkeypatch, "a name")
    assert _text() == "a name"
    _typed(monkeypatch, "")
    assert _text() == "fallback"


def test_no_terminal_means_no_context_at_all(monkeypatch):
    """★ THE ADDITIVE GUARANTEE. "Nobody to ask" is a different outcome from
    "asked, said no", and has to reach the call site as
    ``default_when_unsupported`` — otherwise every hook, pipe and CI run starts
    declining questions it never saw, flipping every call site whose default is
    True into aborting writes it used to make.

    Enforced by not building a context at all, so the ask never gets set up.
    """

    def _fail(_prompt):
        raise AssertionError("must not read stdin when there is no terminal")

    monkeypatch.setattr("sys.stdin", None)
    monkeypatch.setattr("builtins.input", _fail)

    async def takes_ctx(ctx=None):
        return ctx

    assert elicit.with_console_ctx(takes_ctx, {}) == {}
    # …and with ctx=None the helpers take their unsupported branch, both ways.
    assert (
        asyncio.run(elicit.confirm(None, "Proceed?", default_when_unsupported=True))
        is True
    )
    assert (
        asyncio.run(elicit.confirm(None, "Proceed?", default_when_unsupported=False))
        is False
    )


def test_console_ctx_takes_the_backchannel_path_not_mrtr():
    """MRTR is a *wire* mechanism — the client answers by retrying the call —
    and there is no wire here. Pinned because ``_wire_mode`` decides this off a
    ``request_context`` attribute ``ConsoleContext`` deliberately omits."""
    assert elicit._wire_mode(elicit.ConsoleContext()) == elicit._BACKCHANNEL


def test_with_console_ctx_only_fills_a_declared_unset_ctx(monkeypatch):
    async def takes_ctx(slug, ctx=None):
        return ctx

    async def no_ctx(slug):
        return slug

    _typed(monkeypatch, "y")
    assert isinstance(
        elicit.with_console_ctx(takes_ctx, {"slug": "x"})["ctx"], elicit.ConsoleContext
    )
    # A call site that passes its own context still wins.
    assert elicit.with_console_ctx(takes_ctx, {"slug": "x", "ctx": None})["ctx"] is None
    # Injecting into a tool with no ctx parameter would raise TypeError on call.
    assert elicit.with_console_ctx(no_ctx, {"slug": "x"}) == {"slug": "x"}
