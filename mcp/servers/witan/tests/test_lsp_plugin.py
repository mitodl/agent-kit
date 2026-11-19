"""witan-council's language-server plugin and the `witan lsp` entrypoint."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from witan import config as cfg_module
from witan.lsp_plugin import CouncilPlugin
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
        f'server = "{tmp_path / "personal.omni"}"\n'
        f'match_paths = ["{local.parent}"]\n'
    )
    monkeypatch.setenv("WITAN_CONFIG", str(cfg))
    monkeypatch.delenv("WITAN_MEMORY_URI", raising=False)
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    return deployed, local


def _folder(path: Path, target: str | None) -> Folder:
    return Folder(uri=path.as_uri(), path=path, repo=None, branch=None, target=target)


def test_only_the_council_plugin_is_installed_here():
    plugins = discover_plugins()
    assert list(plugins) == ["council"]
    assert isinstance(plugins["council"], CouncilPlugin)
    assert not plugins["council"].capabilities


def test_witan_lsp_starts_and_reports_one_plugin(folders):
    deployed, local = folders
    init, status = asyncio.run(
        spawned_status(
            [sys.executable, "-c", "from witan.cli import main; main()", "lsp"],
            [deployed, local],
        )
    )
    assert init.server_info.name == "witan"
    assert init.capabilities.hover_provider is None
    assert list(init.capabilities.execute_command_provider.commands) == ["witan.status"]
    assert [p["name"] for p in status["plugins"]] == ["council"]
    assert [f["target"] for f in status["folders"]] == ["work", "personal"]


def test_routes_follow_each_folders_target(folders, tmp_path, monkeypatch):
    deployed, local = folders
    monkeypatch.chdir(tmp_path)
    plugin = CouncilPlugin()

    remote = plugin.route(_folder(deployed, "work"))
    assert not remote.local
    assert remote.key.url == "https://witan.example.org/mcp"
    personal = plugin.route(_folder(local, "personal"))
    assert (personal.key, personal.local) == (str(tmp_path / "personal.omni"), True)
    assert personal.guard.deliberate
    hash(personal)


def test_unmatched_folder_routes_by_its_own_path_not_the_cwd(
    folders, tmp_path, monkeypatch
):
    deployed, _ = folders
    stray = _checkout(tmp_path / "stray")
    # The process sits in a deployed checkout; the folder matches nothing.
    monkeypatch.chdir(deployed)
    route = CouncilPlugin().route(_folder(stray, None))
    assert route.local
    assert route.key == cfg_module.load(project_path=stray).graph_uri
    assert cfg_module.load_remote_config(project_path=stray) is None
    assert cfg_module.load_remote_config() is not None


def test_a_deployed_folder_opens_a_remote_proxy(folders):
    from witan.remote.proxy import RemoteServerProxy

    deployed, _ = folders
    plugin = CouncilPlugin()
    folder = _folder(deployed, "work")
    assert isinstance(plugin.open(plugin.route(folder), folder), RemoteServerProxy)


def test_a_local_folder_opens_through_the_local_store_guard(folders, monkeypatch):
    """With a deployment configured, the local path is still guarded, and the
    guard does not import the server (and so open the store) until a read."""
    from witan.cli import local_dispatch

    _, local = folders
    calls = []
    monkeypatch.setattr(
        local_dispatch, "local_server", lambda diagnosis: calls.append(diagnosis)
    )
    plugin = CouncilPlugin()
    folder = _folder(local, "personal")
    plugin.open(plugin.route(folder), folder)
    assert calls[0].target_name == "personal"
    assert calls[0].deployed_targets == ("work",)


def test_folders_on_one_store_keep_their_own_write_guard(tmp_path, monkeypatch):
    """A target that names the default store on purpose, and a folder nothing
    matches, read one store. Only the unmatched folder's writes are guarded."""
    from witan.cli import local_dispatch

    chosen = _checkout(tmp_path / "chosen" / "app")
    stray = _checkout(tmp_path / "stray")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[targets.work]\n"
        'remote_url = "https://witan.example.org/mcp"\n'
        'oidc_issuer = "https://sso.example.org/realms/ol"\n'
        'match_repos = ["https://github.com/work/elsewhere"]\n'
        "[targets.chosen]\n"
        f"server = {json.dumps(str(cfg_module._DEFAULT_GRAPH_URI))}\n"
        f"match_paths = [{json.dumps(str(chosen.parent))}]\n"
    )
    monkeypatch.setenv("WITAN_CONFIG", str(cfg))
    monkeypatch.delenv("WITAN_MEMORY_URI", raising=False)
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.setattr(local_dispatch, "local_server", lambda diagnosis: diagnosis)
    plugin = CouncilPlugin()
    routes = [
        plugin.route(_folder(chosen, "chosen")),
        plugin.route(_folder(stray, None)),
    ]
    assert routes[0].key == routes[1].key
    opened = [plugin.open(r, _folder(stray, None)) for r in routes]
    assert [d.deliberate for d in opened] == [True, False]
