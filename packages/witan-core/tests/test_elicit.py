"""Tests for the shared elicitation primitives.

Moved here from the two servers' test_elicit.py (where confirm/text were
duplicated). Each server keeps only its own composed helper's tests
(``repo_or_detect`` for witan, ``choose_repo`` for witan-code).
"""

import asyncio

from fastmcp.server.elicitation import AcceptedElicitation, DeclinedElicitation

from witan_core import elicit


class _AcceptCtx:
    def __init__(self, data):
        self._data = data
        self.calls = []

    async def elicit(self, message, response_type=None, **kwargs):
        self.calls.append(
            {"message": message, "response_type": response_type, **kwargs}
        )
        return AcceptedElicitation(data=self._data)


class _DeclineCtx:
    async def elicit(self, message, response_type=None, **kwargs):
        return DeclinedElicitation()


class _RaiseCtx:
    async def elicit(self, *args, **kwargs):
        raise RuntimeError("unsupported")


class _HangCtx:
    """Simulates a client that accepts elicitation but never answers — e.g. a
    remote/mobile session with no UI surface to render the prompt on."""

    async def elicit(self, *args, **kwargs):
        await asyncio.sleep(3600)
        raise AssertionError("should have timed out before this returns")


def test_confirm_no_ctx_or_error_returns_default():
    assert (
        asyncio.run(elicit.confirm(None, "q?", default_when_unsupported=True)) is True
    )
    assert (
        asyncio.run(elicit.confirm(None, "q?", default_when_unsupported=False)) is False
    )
    assert (
        asyncio.run(elicit.confirm(_RaiseCtx(), "q?", default_when_unsupported=True))
        is True
    )


def test_confirm_accept_and_decline():
    assert (
        asyncio.run(
            elicit.confirm(_AcceptCtx(True), "q?", default_when_unsupported=False)
        )
        is True
    )
    # accepting with a False value is still a "no"
    assert (
        asyncio.run(
            elicit.confirm(_AcceptCtx(False), "q?", default_when_unsupported=True)
        )
        is False
    )
    assert (
        asyncio.run(elicit.confirm(_DeclineCtx(), "q?", default_when_unsupported=True))
        is False
    )


def test_text_no_ctx_error_or_empty_returns_default():
    assert asyncio.run(elicit.text(None, "q?", default="d")) == "d"
    assert asyncio.run(elicit.text(_RaiseCtx(), "q?", default="d")) == "d"
    assert asyncio.run(elicit.text(_AcceptCtx(""), "q?", default="d")) == "d"
    # whitespace-only is treated as empty → default; a real value is stripped
    assert asyncio.run(elicit.text(_AcceptCtx("   "), "q?", default="d")) == "d"
    assert asyncio.run(elicit.text(_AcceptCtx("  real  "), "q?", default="d")) == "real"


def test_confirm_timeout_returns_default_when_unsupported():
    # A client that never answers (e.g. remote/mobile with no elicitation UI)
    # must not hang the tool call forever — a short timeout degrades it like
    # an unsupported client.
    assert (
        asyncio.run(
            elicit.confirm(
                _HangCtx(),
                "q?",
                default_when_unsupported=True,
                timeout_seconds=0.01,
            )
        )
        is True
    )
    assert (
        asyncio.run(
            elicit.confirm(
                _HangCtx(),
                "q?",
                default_when_unsupported=False,
                timeout_seconds=0.01,
            )
        )
        is False
    )


def test_text_timeout_returns_default():
    assert (
        asyncio.run(elicit.text(_HangCtx(), "q?", default="d", timeout_seconds=0.01))
        == "d"
    )


def test_confirm_forwards_title_as_response_title():
    # A regression here (param renamed, or the forwarding accidentally
    # dropped) would silently fall back to FastMCP's generic "Value" label
    # in the client UI — assert the kwarg actually reaches ctx.elicit().
    ctx = _AcceptCtx(True)
    asyncio.run(
        elicit.confirm(ctx, "q?", default_when_unsupported=False, title="Steal claim?")
    )
    assert ctx.calls[-1]["response_title"] == "Steal claim?"

    # and the default title when the caller doesn't override it
    default_ctx = _AcceptCtx(True)
    asyncio.run(elicit.confirm(default_ctx, "q?", default_when_unsupported=False))
    assert default_ctx.calls[-1]["response_title"] == "Proceed?"


def test_text_forwards_title_as_response_title():
    ctx = _AcceptCtx("answer")
    asyncio.run(elicit.text(ctx, "q?", default="d", title="Repo URI"))
    assert ctx.calls[-1]["response_title"] == "Repo URI"

    default_ctx = _AcceptCtx("answer")
    asyncio.run(elicit.text(default_ctx, "q?", default="d"))
    assert default_ctx.calls[-1]["response_title"] == "Response"
