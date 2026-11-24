"""Logger levels under the language server: WARNING unless WITAN_LOG_LEVEL asks."""

from __future__ import annotations

import logging

import pytest
from pygls.lsp.server import LanguageServer

from witan_core.lsp import server as server_module
from witan_core.observability.logging import configure_logging, reset_logging

_LOGGERS = (
    "pygls.protocol.json_rpc",
    "pygls.feature_manager",
    "httpx2",
    "fastmcp",
    "witan.server",
    "witan_code.server",
)


@pytest.fixture
def levels_at_start(monkeypatch):
    """Run ``serve()`` as ``witan lsp`` does and return each logger's effective
    level at the moment the protocol would start.

    The launcher has already configured logging at INFO by then, and importing
    fastmcp has given its logger its own INFO level, so both are set up here.
    """
    for name in ("WITAN_LOG_LEVEL", "LOG_LEVEL", "WITAN_TARGET"):
        monkeypatch.delenv(name, raising=False)
    # serve() pops these from os.environ; monkeypatch puts back any it removes.
    for name in server_module._AGENT_PATH_ENV_VARS:
        monkeypatch.setenv(name, "unused")
    seen: dict[str, int] = {}

    def start_io(self, *_streams):
        seen.update(
            (name, logging.getLogger(name).getEffectiveLevel()) for name in _LOGGERS
        )

    monkeypatch.setattr(server_module, "discover_plugins", dict)
    monkeypatch.setattr(LanguageServer, "start_io", start_io)

    root = logging.getLogger()
    handlers = root.handlers[:]
    levels = {name: logging.getLogger(name).level for name in (None, *_LOGGERS)}
    reset_logging()
    configure_logging(log_format="json", level="INFO", force=True)
    logging.getLogger("fastmcp").setLevel(logging.INFO)

    def run() -> dict[str, int]:
        server_module.serve()
        return seen

    yield run
    reset_logging()
    root.handlers[:] = handlers
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


def test_every_logger_defaults_to_warning(levels_at_start):
    assert levels_at_start() == dict.fromkeys(_LOGGERS, logging.WARNING)


def test_witan_log_level_restores_verbose_logging(levels_at_start, monkeypatch):
    monkeypatch.setenv("WITAN_LOG_LEVEL", "debug")
    levels = levels_at_start()
    assert levels["pygls.protocol.json_rpc"] == logging.DEBUG
    assert levels["fastmcp"] == logging.DEBUG
    assert levels["witan.server"] == logging.DEBUG
    # The request log stays at its own WARNING, as under every witan command.
    assert levels["httpx2"] == logging.WARNING


def test_bare_log_level_does_not_opt_in(levels_at_start, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    assert levels_at_start() == dict.fromkeys(_LOGGERS, logging.WARNING)
