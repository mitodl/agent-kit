"""A refusal: a tool call declined by design, not a failure of the service.

Admission control, the content scanner, an unprovisioned identity and an
unserved graph all answer a call with "no" on purpose, and the caller is told
why. None of that is an incident.

fastmcp does not see it that way. An ordinary exception out of a tool reaches
``logger.exception`` in ``FastMCP.call_tool``, and ``configure_sentry``
installs ``LoggingIntegration(event_level=ERROR)``, so every refusal became a
Sentry issue with a stack trace. A ``FastMCPError`` takes the other arm: it is
logged at its own ``log_level``, without a traceback, and re-raised as-is, and
the client receives ``str(exc)``. So a refusal IS a ``ToolError`` at WARNING. It
stays a breadcrumb, reaches the caller with its message unchanged, and needs no
wrapper at the tool boundary and no ``before_send`` filter.

It is a base class rather than a conversion at the tool boundary because the
CLI calls tool functions directly (``witan/cli/_common.py:_fn``) and catches
these by their own types. Mixing ``Refusal`` in keeps ``WriteBlocked`` a
``RuntimeError`` there.
"""

from __future__ import annotations

import logging

try:
    from fastmcp.exceptions import ToolError as _Base
except ImportError:  # pragma: no cover - requires the `mcp` extra
    _Base = Exception  # type: ignore[assignment,misc]


class Refusal(_Base):  # type: ignore[misc,valid-type]
    """A tool call declined on purpose. Logged at WARNING, never as an error."""

    # ★ BOTH THIS AND ``__init__`` ARE NEEDED. Subclasses list the builtin first
    # (``class WriteQueueFull(RuntimeError, Refusal)``), and on CPython 3.12
    # ``super().__init__`` from there stops at the builtin, so neither
    # ``FastMCPError.__init__`` nor ours runs and only this class attribute
    # supplies the level. On 3.14 the chain does reach ``FastMCPError.__init__``,
    # which sets the instance attribute to ERROR, and ``__init__`` below puts
    # it back. Without this attribute fastmcp's error path raises
    # AttributeError on 3.12; without ``__init__`` a refusal logs at ERROR on
    # 3.14.
    log_level = logging.WARNING

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.log_level = logging.WARNING
