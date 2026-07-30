"""Additive MCP elicitation primitives, shared by both witan servers.

The connected client may not support elicitation, and many tools also run under
headless automation (context/checkpoint hooks, background indexers). These
helpers keep elicitation strictly *additive*: when the client can't elicit — or
``ctx`` is absent — they return the caller's ``default`` so behavior is exactly
what it was before elicitation existed. Only an *explicit* user decline changes
the outcome.

Two wire mechanisms sit behind that one contract, picked per request:

- **MRTR (2026-07-28, SEP-2322).** The stateless era removed the server→client
  back-channel, so ``ctx.elicit`` raises there. Instead the tool call returns an
  ``input_required`` result carrying the question; the client answers and
  *retries the same call*, and the tool re-runs with the answer in
  ``ctx.input_responses``. The helpers below raise :class:`InputRequired` to
  hand that ask up to :class:`MRTRElicitationMiddleware`, which turns it into
  the wire result. A tool body therefore has to be safe to re-run up to the
  point it asks — which every witan call site already is: each one asks after
  its reads and before its write (``task_claim`` runs a stale-block sweep
  first, which is idempotent).
- **``ctx.elicit`` (handshake eras).** The original server-initiated request,
  unchanged, including the ``timeout_seconds`` guard: some handshake-era
  clients (e.g. a remote/mobile Claude Code session) accept the elicitation
  capability but have no UI surface to render the prompt on, so a real human can
  never answer and the request would hang the tool call — and the whole session
  — forever. That guard is vestigial under MRTR, where an unanswered ask simply
  ends the leg, so it does not apply to that path.

This module is NOT imported by ``witan_core/__init__`` — it depends on
``fastmcp`` (the ``mcp`` extra), so importing the base package stays
dependency-free. Each server composes its own repo-elicitation helpers
(``repo_or_detect`` / ``choose_repo``) on top of these primitives, and registers
:class:`MRTRMiddleware` on its ``FastMCP`` instance.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import FastMCPError
from fastmcp.server.elicitation import AcceptedElicitation
from fastmcp.server.middleware import Middleware

try:  # fastmcp 4.x (MCP SDK v2). 3.4.x has no MRTR — see _wire_mode.
    import mcp_types
    from mcp_types.version import MODERN_PROTOCOL_VERSIONS
except ImportError:  # pragma: no cover — exercised by the 3.4.x half of the pin
    mcp_types = None  # type: ignore[assignment]
    MODERN_PROTOCOL_VERSIONS = frozenset()

if TYPE_CHECKING:
    from fastmcp import Context

DEFAULT_TIMEOUT_SECONDS = 300.0

# Wire mechanism for one ask, decided per request by _wire_mode.
_MRTR = "mrtr"
_BACKCHANNEL = "backchannel"
_UNSUPPORTED = "unsupported"

#: An ask that never reached a human, as distinct from one they answered "no"
#: to. Only the former degrades to the caller's default.
_UNANSWERABLE = object()


class InputRequired(FastMCPError):  # noqa: N818 — a control-flow signal, not an error
    """A guard helper needs client input; the middleware below answers it.

    Raised (never returned) so the helpers keep their ``await confirm(...)``
    call shape while still being able to end the tool leg — under MRTR the ask
    *is* the result of the leg, so the tool body cannot run past this point.
    Carries the answers already collected on earlier rounds as ``request_state``
    so a tool that asks more than once accumulates them instead of re-asking the
    first question forever.

    A ``FastMCPError`` on purpose: FastMCP re-raises those to the middleware
    chain unwrapped and logs them without a traceback, so an ask stays one debug
    line instead of an error dump on every prompt.
    """

    def __init__(
        self, key: str, request: Any, request_state: str | None = None
    ) -> None:
        super().__init__(f"input required: {key}", log_level=logging.DEBUG)
        self.key = key
        self.request = request
        self.request_state = request_state


def _question_key(kind: str, message: str) -> str:
    """A key for one question, stable across the rounds of a retried call.

    The client echoes back the keys the server minted, and the tool re-runs from
    the top on each round, so the key has to be derived from the question rather
    than from call order — a counter would drift the moment a tool takes a
    different branch on a later round. Two identical asks in one call collapse
    onto one key, which is the right answer for a repeated question.
    """
    digest = hashlib.blake2s(f"{kind}\0{message}".encode(), digest_size=8)
    return f"witan-{digest.hexdigest()}"


def _wire_mode(ctx: Context) -> str:
    """Which mechanism carries an ask on this request.

    ``_MRTR`` on a 2026-07-28 connection whose client advertises elicitation;
    ``_UNSUPPORTED`` on that era when it does not (the ask would otherwise fail
    the whole call rather than degrade); ``_BACKCHANNEL`` on the handshake eras,
    where ``ctx.elicit`` still works and reports its own unsupported clients.
    """
    if mcp_types is None:
        return _BACKCHANNEL
    request_context = getattr(ctx, "request_context", None)
    version = getattr(request_context, "protocol_version", None)
    if version not in MODERN_PROTOCOL_VERSIONS:
        return _BACKCHANNEL
    try:
        capabilities = ctx.session.client_capabilities
    except Exception:  # noqa: BLE001 — no live session means nobody to ask
        return _UNSUPPORTED
    if capabilities is None or capabilities.elicitation is None:
        return _UNSUPPORTED
    return _MRTR


def _collected_answers(ctx: Context) -> dict[str, dict]:
    """Every answer this call has gathered so far, keyed by question.

    A retry carries only the round it just answered, so earlier rounds are
    replayed through ``request_state`` — the opaque field the protocol echoes
    back verbatim for exactly this.
    """
    state = getattr(ctx, "request_state", None)
    answers: dict[str, dict] = json.loads(state) if state else {}
    for key, response in (getattr(ctx, "input_responses", None) or {}).items():
        answers[key] = response.model_dump(mode="json")
    return answers


def _ask_over_mrtr(ctx: Context, message: str, kind: str, title: str) -> Any | None:
    """This question's answer, or raise to ask the client for it.

    Returns the accepted scalar; ``None`` for a decline, a cancel, or an accept
    with no content — the caller maps that onto its own default.
    """
    answers = _collected_answers(ctx)
    key = _question_key(kind, message)
    answer = answers.get(key)
    if answer is None:
        raise InputRequired(
            key,
            mcp_types.ElicitRequest(
                params=mcp_types.ElicitRequestFormParams(
                    message=message,
                    requested_schema={
                        "type": "object",
                        "properties": {"value": {"type": kind, "title": title}},
                        "required": ["value"],
                    },
                )
            ),
            # Only carried once there is something to carry: a single-ask tool
            # then never emits request_state, which keeps it independent of the
            # replica that minted it (the server seals the field under a
            # per-process key unless configured with a shared one).
            json.dumps(answers) if answers else None,
        )
    if answer.get("action") != "accept":
        return None
    return (answer.get("content") or {}).get("value")


async def _ask_over_backchannel(
    ctx: Context,
    message: str,
    response_type: type,
    title: str,
    timeout_seconds: float,
) -> Any | None:
    """Same contract as :func:`_ask_over_mrtr`, over the handshake-era request.

    Returns :data:`_UNANSWERABLE` — distinct from a ``None`` decline — when the
    ask never reached a human: an unsupported client, a transport error, or
    nobody answering within ``timeout_seconds``.
    """
    try:
        result = await asyncio.wait_for(
            ctx.elicit(message, response_type=response_type, response_title=title),
            timeout=timeout_seconds,
        )
    except Exception:  # noqa: BLE001
        return _UNANSWERABLE
    if isinstance(result, AcceptedElicitation):
        return result.data
    return None


async def confirm(
    ctx: Context | None,
    message: str,
    *,
    default_when_unsupported: bool,
    title: str = "Proceed?",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """Ask a yes/no question.

    ``accept`` → the chosen bool; ``decline``/``cancel`` → ``False``; and when
    elicitation is unsupported, errors, or times out (headless client, no
    ``ctx``, or no one answers within ``timeout_seconds``) →
    ``default_when_unsupported`` — pick that so the non-interactive path keeps
    today's behavior (e.g. ``False`` for "don't act", ``True`` for "proceed").

    ``title`` labels the boolean field in the client's elicitation form —
    without it the client renders a generic "Value". Pass something specific to
    the question being asked (e.g. "Steal the claim?").
    """
    if ctx is None:
        return default_when_unsupported
    mode = _wire_mode(ctx)
    if mode == _UNSUPPORTED:
        return default_when_unsupported
    if mode == _MRTR:
        answer = _ask_over_mrtr(ctx, message, kind="boolean", title=title)
    else:
        answer = await _ask_over_backchannel(ctx, message, bool, title, timeout_seconds)
    if answer is _UNANSWERABLE:
        return default_when_unsupported
    return bool(answer)


async def text(
    ctx: Context | None,
    message: str,
    *,
    default: str,
    title: str = "Response",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Ask for a line of text. A non-empty accepted value is returned; a
    decline/cancel, an empty value, an unsupported client, a timeout, or no
    ``ctx`` all fall back to ``default``.

    ``title`` labels the text field in the client's elicitation form — see
    ``confirm`` for why this matters.
    """
    if ctx is None:
        return default
    mode = _wire_mode(ctx)
    if mode == _UNSUPPORTED:
        return default
    if mode == _MRTR:
        answer = _ask_over_mrtr(ctx, message, kind="string", title=title)
    else:
        answer = await _ask_over_backchannel(ctx, message, str, title, timeout_seconds)
    if isinstance(answer, str) and answer.strip():
        return answer.strip()
    return default


def _pending_ask(exc: BaseException) -> InputRequired | None:
    """The :class:`InputRequired` behind ``exc``, if the helpers raised one.

    Usually ``exc`` itself. The chain is walked because anything that rewraps a
    tool-body error on the way out — FastMCP's own error masking for a
    non-``FastMCPError``, a mounted server, a transformed tool — keeps the
    original on ``__cause__``, and an ask must survive that to reach the client.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, InputRequired):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


class MRTRElicitationMiddleware(Middleware):
    """Turn a guard helper's ask into an ``input_required`` tool result.

    Register once per server (``mcp.add_middleware(MRTRElicitationMiddleware())``)
    to make :func:`confirm` / :func:`text` work on 2026-07-28 connections. Inert
    on the handshake eras and under fastmcp 3.4.x, where the helpers use
    ``ctx.elicit`` and never raise :class:`InputRequired`.
    """

    async def on_call_tool(self, context, call_next):  # noqa: ANN001, ANN201
        try:
            return await call_next(context)
        except Exception as exc:
            ask = _pending_ask(exc)
            if ask is None:
                raise
            from fastmcp.tools.base import InputRequiredToolResult

            return InputRequiredToolResult(
                mcp_types.InputRequiredResult(
                    input_requests={ask.key: ask.request},
                    request_state=ask.request_state,
                )
            )
