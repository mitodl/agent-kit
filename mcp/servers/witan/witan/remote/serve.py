"""Re-serve a deployed witan's tool surface over local stdio (ADR-0005).

``witan serve`` is what every agent session talks to, and until this module
existed it was the ONE caller that ignored a remote target: ``witan.config.load``
builds ``graph_uri`` from a target's ``server`` field only, so a target that
declares ``remote_url`` and no ``server`` fell through to the default LOCAL
store — silently. The CLI (``witan.cli._common._srv``) routed to the deployment
at the same moment, from the same config, in the same directory, so an agent's
writes and its operator's ``witan`` commands landed in two different graphs with
nothing to say so.

That is the exact failure ``RemoteServerProxy._unreachable_hint`` already
refuses to allow on the CLI side, in its own words: falling back silently
"would split your memory across two graphs with no signal that it happened,
leaving a merge nobody knew to run".

── WHY A LOCAL HOP AT ALL, RATHER THAN POINTING THE AGENT AT THE URL ──
Because the deployment cannot see the caller's checkout. ``_map_args`` fills in
``repo``, ``session_slug`` and ``session_id`` from the local git working copy
before the call goes out; an agent connected straight to the deployed endpoint
would have to know and pass all three itself on every call, and would get the
repo wrong whenever it did not.

── WHY ``code_*`` IS NOT PROXIED, THOUGH THE DEPLOYMENT ADVERTISES IT ──
Indexing reads source files, so witan-code has to run where the checkout is.
Its GRAPH still belongs in the cluster — that is what makes one machine's
in-flight branch visible to another agent session and another developer — but
that is what ``code_transport = "mcp"`` is for: witan-code stays mounted
locally and routes its STORE through the deployment's ``code_store_*`` tools
(``witan_code.store.StoreRef.client`` -> ``RemoteStoreClient``). Republishing
``code_*`` here would send the tool call to a pod with no checkout to read.

So the split is not "memory is shared, code is local". Both graphs live in the
cluster; only the process that reads source files stays on this machine.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools import FunctionTool

from ..config import RemoteConfig
from .oidc import default_token_provider, default_token_refresher
from .proxy import RemoteServerProxy

__all__ = ["build_remote_server", "make_proxy"]


# Tools witan-code serves from THIS machine, which must not be shadowed by a
# forwarding tool of the same name. Everything the deployment advertises under
# this prefix — the `code_*` query tools and the `code_store_*` storage
# primitives alike — is excluded: the query tools are mounted locally right
# after this server is built, and the storage primitives are witan-code's own
# transport to the cluster, called by `RemoteStoreClient` over its own session
# rather than by an agent.
_LOCAL_PREFIX = "code_"


def _forwarder(proxy: RemoteServerProxy, name: str) -> Callable[..., Any]:
    """A tool body that awaits the deployment's answer for ``name``.

    ``**kwargs`` rather than the remote's real parameters because the schema is
    attached to the tool explicitly (see :func:`build_remote_server`) — what
    the agent sees is the deployment's own schema, and what arrives here is
    already validated against it.
    """

    async def call(**kwargs: Any) -> Any:
        # `dispatch`, not `getattr(proxy, name)`: the synchronous form wraps
        # this in `asyncio.run`, which cannot nest inside the event loop
        # FastMCP is already running us on.
        return await proxy.dispatch(name, **kwargs)

    call.__name__ = name
    return call


def make_proxy(remote: RemoteConfig) -> RemoteServerProxy:
    """The dispatching proxy for ``remote``, credentials and all.

    Same construction as ``witan.cli._common._srv`` so a tool called through an
    agent and the same tool called through ``witan …`` on the command line
    reach the deployment identically — including the token refresh that a
    5-minute access token makes routine.
    """
    return RemoteServerProxy(
        remote,
        default_token_provider(remote),
        default_token_refresher(remote),
    )


async def build_remote_server(
    remote: RemoteConfig, *, proxy: RemoteServerProxy | None = None
) -> FastMCP:
    """A FastMCP server whose every tool dispatches to ``remote``.

    The surface is read off the DEPLOYMENT, not generated from local code: the
    deployed release is the authority on what it serves, and a schema built
    here would drift from it at every version skew. A tool this client has
    never heard of is therefore still callable, and one the deployment has
    dropped stops being advertised — both of which a locally-derived surface
    gets wrong.

    ★ RAISES rather than degrading. If the deployment cannot be listed — it is
    unreachable, or the session has expired — this refuses to build a server.
    The alternative is precisely the defect this module exists to remove:
    coming up on the local store while the operator believes their agent is on
    the shared graph. ``witan.cli.serve`` turns the raised error into a message
    naming the endpoint and the way out.
    """
    proxy = proxy or make_proxy(remote)
    mcp: FastMCP = FastMCP("witan")
    for tool in await proxy.remote_tools():
        if tool.name.startswith(_LOCAL_PREFIX):
            continue
        schema = getattr(tool, "input_schema", None)
        if schema is None:
            schema = tool.inputSchema
        mcp.add_tool(
            FunctionTool(
                name=tool.name,
                title=getattr(tool, "title", None),
                description=tool.description,
                parameters=schema,
                fn=_forwarder(proxy, tool.name),
            )
        )
    return mcp
