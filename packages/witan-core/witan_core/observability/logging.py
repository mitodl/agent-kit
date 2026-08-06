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

_configured = False


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
        # ConsoleRenderer formats exc_info tuples itself, so the pipeline only
        # has to make sure exc_info is populated.
        exception_processor: Any = structlog.dev.set_exc_info
        renderer: Any = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
        formatter_processors = [
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ]
    else:
        exception_processor = _EXCEPTION_RENDERER
        renderer = structlog.processors.JSONRenderer()
        # The exception renderer is listed twice on purpose. The pipeline copy
        # handles structlog-native events; the formatter copy handles foreign
        # stdlib records, whose exc_info is attached by ProcessorFormatter after
        # the pipeline has already run. Dropping either one loses tracebacks
        # from that half of the sources.
        formatter_processors = [
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _EXCEPTION_RENDERER,
            renderer,
        ]

    structlog.configure(
        processors=[
            *shared,
            exception_processor,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
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


def get_logger(name: str | None = None) -> Any:
    """A bound structlog logger.

    Callers use this rather than ``structlog.get_logger`` directly so that a
    module importing it before :func:`configure_logging` has run still ends up
    with the configured pipeline — structlog's lazy proxy resolves on first use.
    """
    return structlog.get_logger(name)
