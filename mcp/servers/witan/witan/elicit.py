"""Additive MCP elicitation helpers.

FastMCP 3.4.3 supports ``ctx.elicit``, but the connected client may not, and
several witan tools also run under headless automation (the context/checkpoint
hooks, background indexers). These helpers keep elicitation strictly *additive*:
when the client can't elicit — or ``ctx`` is absent — they return the caller's
``default`` so behavior is exactly what it was before elicitation existed. Only
an *explicit* user decline changes the outcome.

Never call these from a tool that must stay non-interactive under automation
(``workflow_session_start``/``_end``, ``workflow_project_list``, ``memory_store``,
``code_reindex``, ``workflow_trace_mine``) — those never take a ``Context``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp.server.elicitation import AcceptedElicitation

if TYPE_CHECKING:
    from fastmcp import Context


async def confirm(
    ctx: Context | None, message: str, *, default_when_unsupported: bool
) -> bool:
    """Ask a yes/no question.

    ``accept`` → the chosen bool; ``decline``/``cancel`` → ``False``; and when
    elicitation is unsupported or errors (headless client, no ``ctx``) →
    ``default_when_unsupported`` — pick that so the non-interactive path keeps
    today's behavior (e.g. ``False`` for "don't steal", ``True`` for "proceed").
    """
    if ctx is None:
        return default_when_unsupported
    try:
        result = await ctx.elicit(message, response_type=bool)
    except Exception:  # noqa: BLE001 — any elicit failure means "can't ask"
        return default_when_unsupported
    if isinstance(result, AcceptedElicitation):
        return bool(result.data)
    return False


async def text(ctx: Context | None, message: str, *, default: str) -> str:
    """Ask for a line of text. A non-empty accepted value is returned; a
    decline/cancel, an empty value, an unsupported client, or no ``ctx`` all
    fall back to ``default``."""
    if ctx is None:
        return default
    try:
        result = await ctx.elicit(message, response_type=str)
    except Exception:  # noqa: BLE001
        return default
    if isinstance(result, AcceptedElicitation) and result.data:
        return result.data
    return default
