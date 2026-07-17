"""witan's repo-elicitation helper, on top of witan_core's shared primitives.

The ``confirm``/``text`` primitives live in ``witan_core.elicit`` (re-exported
below so existing ``elicit.confirm``/``elicit.text`` call sites keep working).
``repo_or_detect`` is witan-specific — it offers to elicit a *repo* for a write
when detection finds none.

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

from witan_core.elicit import confirm, text

if TYPE_CHECKING:
    from fastmcp import Context

__all__ = ["confirm", "repo_or_detect", "text"]


async def repo_or_detect(ctx: Context | None, repo: str | None) -> str | None:
    """Resolve the repo for a write, offering to elicit one when nothing is known.

    Returns the *resolved* repo so the caller can pass it straight back as the
    write's ``repo`` override — detection then runs only once, not again inside
    the write path:

    - An explicit ``repo`` (caller passed one) is returned untouched.
    - Otherwise git/``WITAN_REPO`` detection runs; a detected repo is returned
      as-is (the caller forwards it as the override, so the write doesn't
      re-detect).
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

    detected = repo_module.detect()
    if detected is not None:
        return detected
    chosen = await text(
        ctx,
        "No repo detected for this write (not in a git repo, or no remote). "
        "Enter a canonical repo URI (e.g. https://github.com/org/name) to scope "
        "it, or leave blank to store it unscoped:",
        default="",
        title="Repo URI",
    )
    return chosen or None
