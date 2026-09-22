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
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import ClassVar

try:
    from fastmcp.exceptions import ToolError as _Base
except ImportError:  # pragma: no cover - requires the `mcp` extra
    _Base = Exception  # type: ignore[assignment,misc]


RESERVED_LOG_FIELDS = frozenset(
    {
        # `mcp.tool_call`'s own keyword arguments. Passing one twice to
        # `log.info` is a TypeError inside the middleware's `finally`, which
        # would replace the caller's refusal with a server fault.
        "event",
        "tool",
        "outcome",
        "duration_ms",
        "actor_id",
        "actor",
        # The failure description `_error_fields` builds.
        "error",
        "error_type",
        "refused",
        "error_withheld",
        "error_count",
        "error_types",
        "error_undescribable",
        # Keys the structlog chain reads or writes. `exc_info` would make the
        # renderer attach the traceback, message and all; the rest are
        # overwritten by a processor or, for `trace_id`, spoofed when no span
        # is active.
        "exc_info",
        "stack_info",
        "timestamp",
        "level",
        "logger",
        "trace_id",
        "span_id",
        "pod_name",
        "namespace",
        "node_name",
    }
)
"""Field names ``Refusal.log_safe_attributes`` may not declare. Names with a
leading underscore (``_record``, ``_from_structlog``) are refused by
:data:`_LOG_FIELD_NAME` instead."""

_LOG_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]*")


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

    log_safe_message = False
    """Whether this refusal's message may go into a structured log field.

    OFF BY DEFAULT, and the default is the point. A refusal's message is
    written to be read by the CALLER, which is not the same as being safe to
    ship to Loki: ``IngestRefused`` interpolates the caller's own step value
    (``got {value!r}``), and until the observability middleware began recording
    failure detail, fastmcp's ``FastMCPError`` arm rendered none of these
    messages, so none of it was exposed. Opting in per type is what keeps a
    newly-written refusal that echoes its input from silently undoing that.

    Set it ``True`` only when the message is built from identifiers (a graph, a
    slug, an actor id, a count) or is masked by that type's own contract, as
    ``scan.enforce.WriteBlocked`` documents. When in doubt leave it off: the
    class NAME reaches the log either way, and the class name alone is what
    turned the 2026-09-16 ``code_store_views`` mystery into a diagnosis.
    """

    log_safe_attributes: ClassVar[Mapping[str, str]] = MappingProxyType({})
    """Structured attributes that may go into the log, as ``{field: attribute}``.

    The way to log the one identifier inside a message that is not safe as a
    whole. ``log_safe_message`` is all-or-nothing on free text, so a type whose
    raise site appends an upstream string stays withheld, and loses the
    identifier with it: ``StoreQuarantined`` carries the sidecar operation id an
    operator needs to quarantine the right file. Declaring
    ``{"sidecar_operation_id": "operation_id"}`` puts that attribute on the
    ``mcp.tool_call`` line as its own field, with none of the prose around it.

    The same bar as ``log_safe_message``, applied to one attribute: declare it
    only when the value is an identifier or a count BY CONSTRUCTION, not merely
    in the cases seen so far. An attribute that is ``None`` is left off the
    line. Independent of ``log_safe_message``; a type may use either or both.

    Field names are checked when the subclass is defined: lowercase, no leading
    underscore, and not in :data:`RESERVED_LOG_FIELDS`. A bad name fails at
    import rather than silently going missing from, or corrupting, the line.
    """

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        bad = sorted(
            field
            for field in cls.log_safe_attributes
            if field in RESERVED_LOG_FIELDS or not _LOG_FIELD_NAME.fullmatch(field)
        )
        if bad:
            msg = (
                f"{cls.__name__}.log_safe_attributes declares {bad}, which the "
                f"mcp.tool_call line reserves or cannot carry"
            )
            raise TypeError(msg)

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.log_level = logging.WARNING
