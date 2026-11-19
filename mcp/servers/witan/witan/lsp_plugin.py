"""witan-council's plugin for the witan language server (ADR 0012).

Registered under the ``witan.lsp.plugins`` entry-point group. It provides no
features yet; it routes each workspace folder through the CLI's own dispatch,
so the council features that follow read the same graph ``witan`` would from
that folder: a ``RemoteServerProxy`` when the folder's target names a
deployment, else the in-process server behind ``local_server``'s guard.

Nothing here imports pygls, so the module loads without the ``lsp`` extra.
"""

from __future__ import annotations

from typing import Any

from witan_core.lsp import Folder, Plugin, Route

from . import config as cfg_module


class CouncilPlugin(Plugin):
    def route(self, folder: Folder) -> Route:
        remote = cfg_module.load_remote_config(
            target=folder.target, project_path=folder.path
        )
        if remote is not None:
            return Route(key=remote, local=False)
        cfg = cfg_module.load(target=folder.target, project_path=folder.path)
        # The guard is per folder: a target that chose this store and a folder
        # that fell back to it read the same graph, but only the second's
        # writes are refused.
        guard = cfg_module.diagnose_local_dispatch(
            target=folder.target, project_path=folder.path
        )
        return Route(key=cfg.graph_uri, local=True, guard=guard)

    def open(self, route: Route, folder: Folder) -> Any:
        if not route.local:
            from .cli._common import remote_proxy

            return remote_proxy(route.key)
        from .cli.local_dispatch import local_server

        return local_server(route.guard)
