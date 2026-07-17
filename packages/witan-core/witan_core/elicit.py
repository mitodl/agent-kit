"""Additive MCP elicitation primitives, shared by both witan servers.

FastMCP supports ``ctx.elicit``, but the connected client may not, and many
tools also run under headless automation (context/checkpoint hooks, background
indexers). These helpers keep elicitation strictly *additive*: when the client
can't elicit — or ``ctx`` is absent — they return the caller's ``default`` so
behavior is exactly what it was before elicitation existed. Only an *explicit*
user decline changes the outcome.

This module is NOT imported by ``witan_core/__init__`` — it depends on
``fastmcp`` (the ``mcp`` extra), so importing the base package stays
dependency-free. Each server composes its own repo-elicitation helpers
(``repo_or_detect`` / ``choose_repo``) on top of these primitives.

Every call is also bounded by a ``timeout_seconds``: some MCP clients (e.g. a
remote/mobile Claude Code session) accept the elicitation capability but have
no UI surface to render the prompt on, so a real human can never answer it —
the request would otherwise hang the tool call, and the whole session,
forever with no visible way to unblock it. A timeout degrades that exactly
like an unsupported client: the caller's default wins, and the tool call
returns instead of hanging.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastmcp.server.elicitation import AcceptedElicitation

if TYPE_CHECKING:
    from fastmcp import Context

DEFAULT_TIMEOUT_SECONDS = 300.0


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
    without it FastMCP falls back to the generic "Value". Pass something
    specific to the question being asked (e.g. "Steal the claim?").
    """
    if ctx is None:
        return default_when_unsupported
    try:
        result = await asyncio.wait_for(
            ctx.elicit(message, response_type=bool, response_title=title),
            timeout=timeout_seconds,
        )
    except Exception:  # noqa: BLE001 — any elicit failure/timeout means "can't ask"
        return default_when_unsupported
    if isinstance(result, AcceptedElicitation):
        return bool(result.data)
    return False


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
    try:
        result = await asyncio.wait_for(
            ctx.elicit(message, response_type=str, response_title=title),
            timeout=timeout_seconds,
        )
    except Exception:  # noqa: BLE001
        return default
    if isinstance(result, AcceptedElicitation) and (result.data or "").strip():
        return result.data.strip()
    return default
