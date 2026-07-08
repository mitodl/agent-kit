"""Additive MCP elicitation helpers.

FastMCP 3.4.3 supports ``ctx.elicit``, but the connected client may not, and
several witan tools also run under headless automation (the context/checkpoint
hooks, background indexers). These helpers keep elicitation strictly *additive*:
when the client can't elicit — or ``ctx`` is absent — they return the caller's
``default`` so behavior is exactly what it was before elicitation existed. Only
an *explicit* user decline changes the outcome.

Never call these from a tool that must stay non-interactive under automation
(``workflow_session_start``/``_end``, ``workflow_project_list``, ``code_reindex``,
``workflow_trace_mine``) — those never take a ``Context``.

``memory_store``/``task_create`` are the exception: they take a ``Context`` *only*
to offer a repo when detection finds none (``repo_or_detect`` below). Because that
helper returns the caller's ``repo`` (``None``) unchanged whenever elicitation is
unsupported or declined, the headless/automation path is byte-for-byte today's
behavior — the additive-only contract still holds.
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


async def repo_or_detect(ctx: Context | None, repo: str | None) -> str | None:
    """Resolve the repo for a write, offering to elicit one when nothing is known.

    - An explicit ``repo`` (caller passed one) is returned untouched.
    - Otherwise, if git/``WITAN_REPO`` detection already yields a repo, return
      ``None`` so the callee's own ``detect()`` resolves it exactly as before.
    - Only when detection finds *nothing* do we prompt for a canonical URI. A
      headless/unsupported client, a decline, or an empty answer all fall back to
      ``None`` — i.e. today's silently-unscoped node — so the additive-only
      contract holds for automation.
    """
    if repo is not None:
        return repo
    # Local import: repo depends only on the stdlib, so no import cycle, and it
    # keeps this module free of a hard dependency for callers that never elicit.
    from . import repo as repo_module

    if repo_module.detect() is not None:
        return None
    chosen = await text(
        ctx,
        "No repo detected for this write (not in a git repo, or no remote). "
        "Enter a canonical repo URI (e.g. https://github.com/org/name) to scope "
        "it, or leave blank to store it unscoped:",
        default="",
    )
    return chosen or None


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
