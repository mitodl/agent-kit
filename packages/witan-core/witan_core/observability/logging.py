"""structlog configuration for the witan servers and CLIs.

Ported from ``mitol.observability.logging`` (ol-django), with the Django settings
lookups replaced by environment variables and one constraint added that a web
framework never has to think about — see "stdout is the protocol" below.

Everything, structlog-native and foreign stdlib alike, terminates in a single
handler whose formatter is a :class:`structlog.stdlib.ProcessorFormatter`. That
is what lets a ``logging.getLogger(...)`` call inside a dependency come out in
the same shape as a ``structlog.get_logger()`` call in our own code.

★ STDOUT IS THE PROTOCOL ★
``witan serve --transport stdio`` speaks MCP over stdout. A single log line
written there corrupts the framing and the session dies, so the handler is
pinned to stderr explicitly. ``logging.StreamHandler()`` happens to default to
stderr, but relying on that default would make the most damaging possible
regression invisible in review — hence the explicit argument and
``test_all_output_goes_to_stderr``.

That covers the *configured* path. The unconfigured one is the sharper edge:
structlog's own out-of-the-box default is ``PrintLoggerFactory()``, which writes
to **stdout**. So a module that logs before — or entirely without —
:func:`configure_logging` would print onto the MCP framing channel, and in the
UserPromptSubmit hook it would land inside the context block the hook writes to
stdout. Both failures are silent and neither shows up in a unit test that only
exercises the configured path. :func:`_install_stderr_default` therefore pins
that fallback to stderr at import, and :func:`reset_logging` restores it, so
there is no reachable state in which a witan logger targets stdout. See
``test_unconfigured_logger_never_writes_to_stdout``.
"""

from __future__ import annotations

import logging
import logging.config
import os
import sys
from typing import Any

import structlog

from witan_core.observability.processors import inject_k8s_context, inject_otel_context

# One shared instance: the same renderer has to appear both in the structlog
# pipeline and in the ProcessorFormatter, and constructing two would let their
# configuration drift apart silently.
_EXCEPTION_RENDERER = structlog.processors.ExceptionRenderer(
    structlog.processors.ExceptionDictTransformer(show_locals=False, max_frames=20)
)

# Libraries that log at INFO on every call and would drown the signal. Matches
# the reference package's list, minus its Django/Celery entries.
_NOISY_LOGGERS = ("botocore", "boto3", "urllib3", "httpx", "httpcore", "hpack")


def _wrap_for_formatter_preserving_exc_info(
    logger: Any, name: str, event_dict: Any
) -> Any:
    """Like ``ProcessorFormatter.wrap_for_formatter``, but forwards ``exc_info``
    onto the stdlib ``LogRecord`` it produces.

    structlog's own ``wrap_for_formatter`` packages the whole event dict as a
    single positional arg and does not forward ``exc_info``/``stack_info`` to
    the record at all -- by design, since it expects *this formatter's own*
    processor chain (``formatter_processors`` below) to re-render the
    exception from the event dict at format time, not from ``record.exc_info``.
    That leaves ``record.exc_info`` permanently ``None`` for every
    structlog-native call: invisible to anything that inspects the raw stdlib
    record for it, which is exactly what Sentry's ``LoggingIntegration`` does
    (see ``observability.telemetry.configure_sentry``) -- it would see an
    ERROR record with no exception and report a bare message, silently
    discarding the traceback and breaking issue grouping. Forwarding it here
    costs nothing for JSON/console rendering: the pipeline no longer consumes
    ``exc_info`` before this point (see ``set_exc_info`` in
    :func:`configure_logging`), so it is still present in the event dict for
    the formatter's own ``_EXCEPTION_RENDERER`` pass to render exactly once.
    """
    args, kwargs = structlog.stdlib.ProcessorFormatter.wrap_for_formatter(
        logger, name, event_dict
    )
    exc_info = event_dict.get("exc_info")
    if exc_info:
        kwargs["exc_info"] = exc_info
    return args, kwargs


_configured = False


class _LateBoundStderr:
    """A write target that resolves ``sys.stderr`` per call.

    ``PrintLoggerFactory(file=sys.stderr)`` would capture whatever object
    ``sys.stderr`` names at import. That is the wrong moment: pytest's capsys,
    ``contextlib.redirect_stderr`` and any CLI that rebinds the stream would all
    be writing somewhere this logger no longer points, and the safety net would
    be silently aimed at a dead file object. Resolving on each write keeps it
    aimed at whatever stderr currently is — while never being stdout.
    """

    def write(self, message: str) -> int:
        return sys.stderr.write(message)

    def flush(self) -> None:
        sys.stderr.flush()


def _install_stderr_default() -> None:
    """Point structlog's unconfigured fallback at stderr instead of stdout.

    Only the ``logger_factory`` is set, deliberately: this is a safety net, not
    a configuration. Leaving the processor chain alone means an unconfigured
    log line still renders readably, and :func:`configure_logging` remains the
    single place that decides format, level and routing.
    """
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=_LateBoundStderr())
    )


_install_stderr_default()


def _log_level() -> str:
    """The configured level, ``WITAN_LOG_LEVEL`` taking precedence.

    ``LOG_LEVEL`` is the ol-django convention and is honored so a deployment can
    set one variable for every service in the namespace; the witan-specific name
    exists to turn one chatty server up without doing the same to its neighbors.
    """
    level = os.environ.get("WITAN_LOG_LEVEL") or os.environ.get("LOG_LEVEL") or "INFO"
    return level.upper()


def _log_format() -> str:
    """``json`` or ``console``.

    The default is chosen by asking whether stderr is a terminal rather than by a
    DEBUG flag, because witan is a server and a CLI in the same process tree: the
    deployed pod pipes stderr to a collector and wants JSON, a developer running
    the same binary wants colors, and neither should have to pass a flag.
    """
    fmt = os.environ.get("WITAN_LOG_FORMAT")
    if fmt:
        return fmt.lower()
    return "console" if sys.stderr.isatty() else "json"


def _shared_processors() -> list[Any]:
    """Processors applied to structlog-native and foreign stdlib records alike."""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.PositionalArgumentsFormatter(),
        inject_otel_context,
        inject_k8s_context,
        structlog.processors.StackInfoRenderer(),
    ]


def configure_logging(
    *,
    log_format: str | None = None,
    level: str | None = None,
    force: bool = False,
) -> None:
    """Configure structlog and route stdlib logging through it.

    Idempotent: repeated calls are ignored unless ``force`` is set. Both the
    umbrella ``witan serve`` and ``witan-code serve`` call this, and mounting one
    server into the other means both can run in a single process.
    """
    global _configured  # noqa: PLW0603 - module-level once-only guard
    if _configured and not force:
        return

    fmt = (log_format or _log_format()).lower()
    shared = _shared_processors()

    if fmt == "console":
        # ConsoleRenderer formats exc_info tuples itself.
        renderer: Any = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
        formatter_processors = [
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ]
    else:
        renderer = structlog.processors.JSONRenderer()
        # The exception renderer runs here, at format time, for BOTH sources:
        # a foreign stdlib record's exc_info is attached by ProcessorFormatter
        # right before this chain runs, and a structlog-native record's
        # exc_info survives to this point too -- the pipeline below only
        # *normalizes* it (set_exc_info), it does not render/consume it early.
        # Rendering it any earlier is what silently dropped exc_info off the
        # stdlib LogRecord for every native call; see
        # _wrap_for_formatter_preserving_exc_info.
        formatter_processors = [
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _EXCEPTION_RENDERER,
            renderer,
        ]

    structlog.configure(
        processors=[
            *shared,
            # Normalizes exc_info=True to a real (type, value, traceback)
            # tuple without rendering/consuming it -- rendering happens once,
            # in formatter_processors above, at format time. This is what
            # keeps exc_info alive long enough for
            # _wrap_for_formatter_preserving_exc_info to forward it onto the
            # stdlib LogRecord.
            structlog.dev.set_exc_info,
            _wrap_for_formatter_preserving_exc_info,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=formatter_processors,
        foreign_pre_chain=shared,
    )
    # See "STDOUT IS THE PROTOCOL" in the module docstring — stderr is explicit.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "handlers": {"default": {"()": lambda: handler}},
            "root": {"handlers": ["default"], "level": level or _log_level()},
            "loggers": {
                name: {"level": "WARNING", "propagate": True} for name in _NOISY_LOGGERS
            },
        }
    )
    _configured = True


def reset_logging() -> None:
    """Clear the once-only guard. For tests."""
    global _configured  # noqa: PLW0603 - module-level once-only guard
    _configured = False
    structlog.reset_defaults()
    # reset_defaults() restores structlog's stdout-writing factory, so the
    # stderr pin has to be reapplied — otherwise every test that resets logging
    # leaves the process one log call away from corrupting stdio, which is the
    # exact hazard this guards.
    _install_stderr_default()


def get_logger(name: str | None = None) -> Any:
    """A bound structlog logger.

    Callers use this rather than ``structlog.get_logger`` directly so that a
    module importing it before :func:`configure_logging` has run still ends up
    with the configured pipeline — structlog's lazy proxy resolves on first use.
    """
    return structlog.get_logger(name)
