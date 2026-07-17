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
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp.server.elicitation import AcceptedElicitation

if TYPE_CHECKING:
    from fastmcp import Context


async def confirm(
    ctx: Context | None,
    message: str,
    *,
    default_when_unsupported: bool,
    title: str = "Proceed?",
) -> bool:
    """Ask a yes/no question.

    ``accept`` → the chosen bool; ``decline``/``cancel`` → ``False``; and when
    elicitation is unsupported or errors (headless client, no ``ctx``) →
    ``default_when_unsupported`` — pick that so the non-interactive path keeps
    today's behavior (e.g. ``False`` for "don't act", ``True`` for "proceed").

    ``title`` labels the boolean field in the client's elicitation form —
    without it FastMCP falls back to the generic "Value". Pass something
    specific to the question being asked (e.g. "Steal the claim?").
    """
    if ctx is None:
        return default_when_unsupported
    try:
        result = await ctx.elicit(message, response_type=bool, response_title=title)
    except Exception:  # noqa: BLE001 — any elicit failure means "can't ask"
        return default_when_unsupported
    if isinstance(result, AcceptedElicitation):
        return bool(result.data)
    return False


async def text(
    ctx: Context | None, message: str, *, default: str, title: str = "Response"
) -> str:
    """Ask for a line of text. A non-empty accepted value is returned; a
    decline/cancel, an empty value, an unsupported client, or no ``ctx`` all
    fall back to ``default``.

    ``title`` labels the text field in the client's elicitation form — see
    ``confirm`` for why this matters.
    """
    if ctx is None:
        return default
    try:
        result = await ctx.elicit(message, response_type=str, response_title=title)
    except Exception:  # noqa: BLE001
        return default
    if isinstance(result, AcceptedElicitation) and (result.data or "").strip():
        return result.data.strip()
    return default
