"""Tools the server keeps callable but lists only to witan's own CLI.

These are admin, migration and corpus-mining operations a person runs on
purpose through ``witan migrate`` and ``witan trace``. Agents almost never call
them (zero to three calls in 30 days on the deployment), yet a listed tool
costs every session discovery: ``claim_authorship`` was loaded by ToolSearch in
83 sessions through the keyword "claim" and called in none.

They cannot simply be unregistered. Against a deployed target the CLI reaches
the graph only through ``RemoteServerProxy``, which dispatches every call over
MCP and learns each tool's parameter names from ``tools/list``. A tool absent
from the protocol surface is therefore absent from the CLI too. So they stay
registered, ``tools/call`` still resolves them by name, and
:class:`CliOnlyToolsMiddleware` leaves them out of ``tools/list`` for any client
that is not the proxy. ``witan serve`` re-serving a deployment is the proxy's
one caller that republishes the list to an agent, so
``witan.remote.serve.build_remote_server`` drops them as well.

The gate is the client's self-reported ``clientInfo`` name. That is a
discoverability filter, not access control: any authorized caller that names
one of these tools can still call it, exactly as before.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastmcp.server.middleware import Middleware
from fastmcp.tools import Tool

from witan_core.remote.proxy import proxy_client_info

__all__ = ["CLI_ONLY_TOOLS", "CliOnlyToolsMiddleware"]

CLI_ONLY_TOOLS = frozenset(
    {
        "claim_authorship",
        "store_merge",
        "workflow_trace_annotate",
        "workflow_trace_get",
        "workflow_trace_list",
        "workflow_trace_mine",
    }
)

_PROXY_CLIENT_NAME = proxy_client_info().name


def _from_witan_proxy(context: Any) -> bool:
    ctx = context.fastmcp_context
    if ctx is None or ctx.request_context is None:
        return False
    info = getattr(ctx.session.client_params, "client_info", None)
    return info is not None and info.name == _PROXY_CLIENT_NAME


class CliOnlyToolsMiddleware(Middleware):
    """Leave :data:`CLI_ONLY_TOOLS` out of ``tools/list`` unless the CLI asks."""

    async def on_list_tools(self, context, call_next) -> Sequence[Tool]:
        tools = await call_next(context)
        if _from_witan_proxy(context):
            return tools
        return [tool for tool in tools if tool.name not in CLI_ONLY_TOOLS]
