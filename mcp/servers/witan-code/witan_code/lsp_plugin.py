"""witan-code's plugin for the witan language server (ADR 0012).

Registered under the ``witan.lsp.plugins`` entry-point group. It provides no
features yet; it routes each workspace folder the way ``witan-code`` would
from that folder, so the code features that follow read the same graph: a
``RemoteServerProxy`` when the folder's target names a deployment, else the
in-process server reading that target's local stores.

Nothing here imports pygls, so the module loads without the ``lsp`` extra.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from witan_core.lsp import Folder, Plugin, Route

from . import config as cfg_module


def _config(folder: Folder) -> cfg_module.Config:
    """The config ``witan-code`` run from ``folder`` would use. A relative
    ``code_dir`` is relative to the cwd, which for the CLI is the folder."""
    cfg = cfg_module.load(folder.target, project_path=folder.path)
    return replace(cfg, code_dir=folder.path / cfg.code_dir)


class CodePlugin(Plugin):
    def route(self, folder: Folder) -> Route:
        remote = cfg_module.load_remote_config(folder.target, project_path=folder.path)
        if remote is not None:
            return Route(key=remote, local=False)
        cfg = _config(folder)
        return Route(key=(str(cfg.code_dir), cfg.code_server), local=True)

    def open(self, route: Route, folder: Folder) -> Any:
        if not route.local:
            from .cli import remote_proxy

            return remote_proxy(route.key)
        from . import server as server_module

        server_module.use_config(_config(folder))
        return server_module
