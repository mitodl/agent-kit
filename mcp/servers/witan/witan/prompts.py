"""Turning off a tool's terminal prompts for a stretch of client code.

Both CLI dispatch paths put a tool's asks to the person at the terminal when
stdin is a tty: ``_fn`` hands an in-process tool a ``ConsoleContext``, and the
remote proxy advertises ``console_elicitation_handler``. Code whose output is
data cannot allow that. The launch spec's claim is the case: under
``witan task run --emit-spec`` from a shell, ``task_claim``'s steal offer
would write its question to stdout ahead of the JSON, and block on an answer
the person never sees. Inside :func:`prompts_off` both paths behave as if
there were no terminal, so each ask takes its ``default_when_unsupported``.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

_off: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "witan_prompts_off", default=False
)


@contextmanager
def prompts_off() -> Iterator[None]:
    """Suppress terminal prompts from tools called inside the block."""
    token = _off.set(True)
    try:
        yield
    finally:
        _off.reset(token)


def prompts_allowed() -> bool:
    """Whether a tool called now may prompt at the terminal.

    :returns: ``False`` inside :func:`prompts_off`.
    :rtype: bool
    """
    return not _off.get()
