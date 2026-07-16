"""witan-code's repo-narrowing helper, on top of witan_core's shared primitives.

The ``confirm``/``text`` primitives live in ``witan_core.elicit`` (re-exported
below so existing ``elicit.confirm``/``elicit.text`` call sites keep working).
``choose_repo`` is witan-code-specific — it narrows a multi-repo name match.

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

from witan_core.elicit import confirm, text

if TYPE_CHECKING:
    from fastmcp import Context

__all__ = ["choose_repo", "confirm", "text"]


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
