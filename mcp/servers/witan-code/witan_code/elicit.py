"""Additive MCP elicitation helpers for the witan-code tools.

FastMCP 3.4.3 supports ``ctx.elicit``, but the connected client may not, and
several ``code_*`` tools also run under headless automation (background
indexers, other MCP clients without elicitation support). These helpers keep
elicitation strictly *additive*: when the client can't elicit — or ``ctx`` is
absent — they return the caller's ``default`` so behavior is exactly what it
was before elicitation existed. Only an *explicit* user decline changes the
outcome.

Call sites in ``server.py``:

- ``code_symbols_in_file`` offers to elicit a repo URI when none can be
  detected (``text``).
- ``code_find_references``/``code_callers``/``code_impact``/
  ``code_symbols_in_file`` and the bridge-backed interface/cross-repo tools
  offer to index now when the relevant store is missing (``confirm``, via the
  ``_confirm_and_reindex``/``_confirm_and_reindex_bridge`` helpers in
  ``server.py``).
- ``code_find_definition`` offers to narrow a multi-repo name match down to
  one repo (``choose_repo``).

Never call these from a tool that must stay non-interactive under automation
— ``code_reindex`` never takes a ``Context`` (see its docstring), and
``code_search_symbol`` returns many results by design, not an error case.
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
    today's behavior (here, always ``False``: never index without being asked).
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
    if isinstance(result, AcceptedElicitation) and (result.data or "").strip():
        return result.data.strip()
    return default


async def choose_repo(
    ctx: Context | None, message: str, repos: list[str]
) -> str | None:
    """Ask the caller to narrow a multi-repo match down to one of ``repos``.

    Reuses ``text`` for the primitive. The answer is matched against ``repos``
    case-insensitively and whitespace-stripped; an exact match returns that
    repo, and anything else — empty, declined, unsupported, or a value that
    doesn't match any candidate — returns ``None``, meaning "keep every
    match" (today's behavior, unchanged).
    """
    answer = await text(ctx, message, default="")
    if not answer:
        return None
    normalized = answer.strip().casefold()
    for repo in repos:
        if repo.casefold() == normalized:
            return repo
    return None
