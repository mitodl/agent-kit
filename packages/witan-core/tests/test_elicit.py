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

    async def elicit(self, message, response_type=None, **kwargs):
        return AcceptedElicitation(data=self._data)


class _DeclineCtx:
    async def elicit(self, message, response_type=None, **kwargs):
        return DeclinedElicitation()


class _RaiseCtx:
    async def elicit(self, *args, **kwargs):
        raise RuntimeError("unsupported")


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
