"""Tests for the shared elicitation primitives.

Moved here from the two servers' test_elicit.py (where confirm/text were
duplicated). Each server keeps only its own composed helper's tests
(``repo_or_detect`` for witan, ``choose_repo`` for witan-code).

The first half drives the helpers against hand-rolled ``ctx`` stand-ins, which
have no ``request_context`` and so exercise the handshake-era ``ctx.elicit``
path. The second half runs them over a real client/server pair on *both*
protocol eras, since the 2026-07-28 mechanism (MRTR) is a different wire path
reaching the same contract — and only an end-to-end run covers the retry.
"""

import asyncio

import pytest
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


# ── over the wire, on both protocol eras ──────────────────────────────────
# 2026-07-28 removed the server→client back-channel, so `ctx.elicit` raises
# there and the helpers ask by returning an `input_required` result the client
# answers and retries. Both eras must produce the same answers from the same
# call sites; anything less is a silent regression to defaults on the era the
# deployment actually runs.

ERAS = ["2026-07-28", "legacy"]


def _server():
    """A FastMCP server whose tools ask through the helpers under test."""
    from fastmcp import Context, FastMCP

    mcp = FastMCP("elicit-test")
    mcp.add_middleware(elicit.MRTRElicitationMiddleware())

    @mcp.tool
    async def ask_once(fallback: bool, ctx: Context) -> dict:
        """Confirm, then report what the caller said.

        ``fallback`` is the value an un-askable client gets, chosen per test so
        the answer distinguishes "the human said this" from "nobody was asked".
        """
        return {
            "ok": await elicit.confirm(
                ctx, "Proceed?", default_when_unsupported=fallback, title="Go?"
            )
        }

    @mcp.tool
    async def ask_twice(ctx: Context) -> dict:
        """Two asks in one call — each round has to remember the last."""
        return {
            "a": await elicit.text(ctx, "First?", default="da", title="A"),
            "b": await elicit.text(ctx, "Second?", default="db", title="B"),
        }

    return mcp


async def _accept(message, _response_type, params, _ctx):
    """Answer every ask: True for a boolean question, an echo for a text one."""
    from fastmcp.client.elicitation import ElicitResult

    field = params.requested_schema["properties"]["value"]
    value = True if field["type"] == "boolean" else f"said-{message}"
    return ElicitResult(action="accept", content={"value": value})


async def _decline(_message, _response_type, _params, _ctx):
    from fastmcp.client.elicitation import ElicitResult

    return ElicitResult(action="decline")


def _run(era, handler, tool, **arguments):
    from fastmcp import Client

    async def _call():
        async with Client(_server(), elicitation_handler=handler, mode=era) as client:
            return (await client.call_tool(tool, arguments)).data

    return asyncio.run(_call())


@pytest.mark.parametrize("era", ERAS)
def test_accepted_answers_reach_the_tool(era):
    # Both answers are the opposite of what an un-askable client would get, so
    # they can only come from the round trip actually happening.
    assert _run(era, _accept, "ask_once", fallback=False) == {"ok": True}
    assert _run(era, _accept, "ask_twice") == {
        "a": "said-First?",
        "b": "said-Second?",
    }


@pytest.mark.parametrize("era", ERAS)
def test_decline_is_a_no_not_a_default(era):
    # The fallback is True, so False proves the decline was heard rather than
    # the ask having quietly degraded.
    assert _run(era, _decline, "ask_once", fallback=True) == {"ok": False}
    assert _run(era, _decline, "ask_twice") == {"a": "da", "b": "db"}


@pytest.mark.parametrize("era", ERAS)
def test_client_that_cannot_elicit_gets_the_defaults(era):
    # A client with no handler doesn't advertise the capability. Asking anyway
    # would fail the whole call on 2026-07-28 (the ask has nowhere to go), so
    # the helpers must degrade instead — additive-only, as under automation.
    assert _run(era, None, "ask_once", fallback=True) == {"ok": True}
    assert _run(era, None, "ask_once", fallback=False) == {"ok": False}
    assert _run(era, None, "ask_twice") == {"a": "da", "b": "db"}
