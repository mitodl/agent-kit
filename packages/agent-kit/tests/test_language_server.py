"""The meta-package installs a language server that finds both witan plugins."""

import asyncio
import sys

import pytest

from witan_core.lsp.plugin import discover_plugins
from witan_core.lsp.testing import spawned_status


def test_both_plugins_are_installed():
    assert list(discover_plugins()) == ["code", "council"]


@pytest.mark.parametrize(
    "entrypoint",
    ["from witan.cli import main; main()", "from witan_code.cli import cli; cli()"],
)
def test_either_entrypoint_serves_both_plugins(entrypoint, tmp_path):
    init, status = asyncio.run(
        spawned_status([sys.executable, "-c", entrypoint, "lsp"], [tmp_path])
    )
    assert init.server_info.name == "witan"
    assert [p["name"] for p in status["plugins"]] == ["code", "council"]
