"""A drop-in stand-in for the ``witan.server`` module that dispatches each
tool call over MCP to the deployed witan service (ADR 0005, path a).

``witan.cli._common._srv()`` returns an instance of this when
``WITAN_REMOTE_URL`` is set, so every existing CLI call site —
``_fn(s.task_ready)(repo=…)`` — transparently becomes an authenticated MCP
``call_tool`` against the deployment, with the deployed server doing the
ADR-0004 JWT→actor→token mapping. Nothing in the ~40 call sites changes.

Two shape details make that transparency work:

- FastMCP's ``CallToolResult.data`` already unwraps the ``{"result": …}``
  output-schema envelope back to the raw ``list``/``dict`` an in-process call
  returns, so the CLI's rendering code is untouched.
- CLI sites pass the first argument positionally (``s.task_get(slug)``); the
  MCP protocol is keyword-only. We map positionals to names using the tool's
  ``inputSchema`` property order, which FastMCP derives from the function
  signature (verified: property order == signature order).
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable

from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastmcp.client.transports import StreamableHttpTransport

from .. import repo as repo_module
from ..config import RemoteConfig

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


class RemoteToolUnavailable(RuntimeError):
    """Raised when a CLI command has no remotely-callable counterpart."""


class RemoteServerProxy:
    """Mirrors the ``witan.server`` tool surface, dispatching over MCP."""

    def __init__(self, cfg: RemoteConfig, token_provider: Callable[[], str]) -> None:
        self._cfg = cfg
        self._token_provider = token_provider
        self._param_names: dict[str, list[str]] | None = None
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> Callable[..., Any]:
        # Real instance attributes (_cfg, _token_provider, …) are set in
        # __init__ and never reach here; dunders must not be intercepted.
        if name.startswith("__"):
            raise AttributeError(name)
        if name in _ADMIN_ONLY:

            def _admin(*_args: Any, **_kwargs: Any) -> Any:
                raise RemoteToolUnavailable(
                    f"`{name}` is an in-cluster admin operation, not available "
                    "over the remote CLI. Run it inside the cluster as "
                    "svc-witan-admin (ADR-0005 path b) — e.g. via a maintenance "
                    "Job or `kubectl exec`."
                )

            return _admin

        def _call(*args: Any, **kwargs: Any) -> Any:
            return asyncio.run(self._invoke(name, args, kwargs))

        return _call

    def _new_client(self, token: str) -> Client:
        """Build an MCP client authenticated with the caller's JWT.

        Isolated so tests can point the proxy at an in-memory FastMCP server.
        """
        transport = StreamableHttpTransport(self._cfg.url, auth=BearerAuth(token))
        return Client(transport)

    async def _invoke(self, name: str, args: tuple, kwargs: dict) -> Any:
        token = self._token_provider()
        async with self._new_client(token) as client:
            if self._param_names is None:
                with self._lock:
                    if self._param_names is None:
                        self._param_names = {
                            t.name: list(t.inputSchema.get("properties", {}).keys())
                            for t in await client.list_tools()
                        }
            arguments = self._map_args(name, args, kwargs)
            result = await client.call_tool(name, arguments)
            return result.data

    def _map_args(self, name: str, args: tuple, kwargs: dict) -> dict:
        assert self._param_names is not None
        names = self._param_names.get(name)
        if names is None:
            raise RemoteToolUnavailable(
                f"The deployed witan service exposes no `{name}` tool. "
                "(Admin/migration commands run in-cluster — see ADR-0005.)"
            )
        arguments = dict(kwargs)
        for i, val in enumerate(args):
            arguments[names[i]] = val
        # The deployed server has no git checkout, so it cannot resolve
        # repo=None ("detect current repo") — do that on the client and send an
        # explicit value. repo="" (all repos) is a meaningful sentinel, kept.
        if "repo" in names and arguments.get("repo") is None:
            detected = repo_module.detect()
            if detected is not None:
                arguments["repo"] = detected
            else:
                arguments.pop("repo", None)
        # Drop remaining None values so omitted optionals take the tool's own
        # default rather than being sent as explicit nulls.
        return {k: v for k, v in arguments.items() if v is not None}
