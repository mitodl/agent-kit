"""witan-code's language-server plugin and the `witan-code lsp` entrypoint."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from witan_code import config as cfg_module
from witan_code.lsp_plugin import CodePlugin
from witan_core.lsp import Folder
from witan_core.lsp.plugin import discover_plugins
from witan_core.lsp.testing import spawned_status


def _checkout(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


@pytest.fixture
def folders(tmp_path, monkeypatch):
    """A deployed target and a local one, each matching a checkout by path."""
    deployed = _checkout(tmp_path / "work" / "app")
    local = _checkout(tmp_path / "personal" / "dots")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[targets.work]\n"
        'remote_url = "https://witan.example.org/mcp"\n'
        'oidc_issuer = "https://sso.example.org/realms/ol"\n'
        f'match_paths = ["{deployed.parent}"]\n'
        "[targets.personal]\n"
        f'code_dir = "{tmp_path / "personal-code"}"\n'
        f'match_paths = ["{local.parent}"]\n'
    )
    monkeypatch.setenv("WITAN_CONFIG", str(cfg))
    monkeypatch.delenv("WITAN_CODE_DIR", raising=False)
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    return deployed, local


def _folder(path: Path, target: str | None) -> Folder:
    return Folder(uri=path.as_uri(), path=path, repo=None, branch=None, target=target)


def test_only_the_code_plugin_is_installed_here():
    plugins = discover_plugins()
    assert list(plugins) == ["code"]
    assert isinstance(plugins["code"], CodePlugin)
    assert not plugins["code"].capabilities


def test_witan_code_lsp_starts_and_reports_one_plugin(folders):
    deployed, local = folders
    init, status = asyncio.run(
        spawned_status(
            [sys.executable, "-c", "from witan_code.cli import cli; cli()", "lsp"],
            [deployed, local],
        )
    )
    assert init.server_info.name == "witan"
    assert init.capabilities.hover_provider is None
    assert [p["name"] for p in status["plugins"]] == ["code"]
    assert [f["target"] for f in status["folders"]] == ["work", "personal"]


def test_routes_follow_each_folders_target(folders, tmp_path, monkeypatch):
    deployed, local = folders
    monkeypatch.chdir(deployed)
    plugin = CodePlugin()

    remote = plugin.route(_folder(deployed, "work"))
    assert not remote.local
    assert remote.key.url == "https://witan.example.org/mcp"
    personal = plugin.route(_folder(local, "personal"))
    assert personal.local
    assert personal.key == (str(tmp_path / "personal-code"), None)
    stray = _checkout(tmp_path / "stray")
    assert plugin.route(_folder(stray, None)).key == (
        str(cfg_module.load(project_path=stray).code_dir),
        None,
    )
    assert cfg_module.load_remote_config(project_path=stray) is None


def test_a_local_folder_points_the_server_at_its_own_stores(folders, tmp_path):
    from witan_code import server as server_module

    _, local = folders
    plugin = CodePlugin()
    folder = _folder(local, "personal")
    before = server_module.cfg
    try:
        assert plugin.open(plugin.route(folder), folder) is server_module
        assert server_module.cfg.code_dir == tmp_path / "personal-code"
    finally:
        server_module.use_config(before)


def test_a_deployed_folder_opens_a_remote_proxy(folders):
    from witan_code.remote.proxy import RemoteServerProxy

    deployed, _ = folders
    plugin = CodePlugin()
    folder = _folder(deployed, "work")
    assert isinstance(plugin.open(plugin.route(folder), folder), RemoteServerProxy)
