"""witan's binding of the shared MCP-client proxy (ADR 0005, path a).

The transport, argument mapping, and result-envelope unwrapping live in
:class:`witan_core.remote.proxy.RemoteMCPProxy`; :class:`RemoteServerProxy` here
binds witan's policy — which tools are in-cluster admin/break-glass ops to refuse
(:data:`_ADMIN_ONLY`), how ``repo=None`` is resolved client-side, and the exact
refusal wording — so ``witan.cli._common._srv()`` gets a drop-in stand-in for the
``witan.server`` module. Nothing in the ~40 CLI call sites changes; the deployed
server does the ADR-0004 JWT→actor→token mapping.
"""

from __future__ import annotations

from typing import Callable

from witan_core.remote.proxy import RemoteMCPProxy, RemoteToolUnavailable

from .. import repo as repo_module
from ..config import RemoteConfig

__all__ = ["RemoteServerProxy", "RemoteToolUnavailable"]

# In-process-only module functions (deliberately not @mcp.tool): schema/
# migration/merge admin ops with no per-user identity. They belong to the
# in-cluster svc-witan-admin path (ADR-0005 path b), never the remote CLI.
_ADMIN_ONLY = frozenset(
    {
        "apply_schema",
        "migrate_topics",
        "migrate_storage_format",
        "merge_store",
        "_topic_schema_present",
    }
)


class RemoteServerProxy(RemoteMCPProxy):
    """Mirrors the ``witan.server`` tool surface, dispatching over MCP."""

    def __init__(self, cfg: RemoteConfig, token_provider: Callable[[], str]) -> None:
        super().__init__(cfg.url, token_provider)

    def _is_admin_tool(self, name: str) -> bool:
        return name in _ADMIN_ONLY

    def _admin_error(self, name: str) -> str:
        return (
            f"`{name}` is an in-cluster admin operation, not available over the "
            "remote CLI. Run it inside the cluster as svc-witan-admin (ADR-0005 "
            "path b) — e.g. via a maintenance Job or `kubectl exec`."
        )

    def _unknown_tool_error(self, name: str) -> str:
        return (
            f"The deployed witan service exposes no `{name}` tool. "
            "(Admin/migration commands run in-cluster — see ADR-0005.)"
        )

    def _resolve_repo(self) -> str | None:
        return repo_module.detect()
