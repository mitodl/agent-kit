"""A drop-in stand-in for an in-process server module that dispatches each tool
call over MCP to a deployed FastMCP service (ADR-0005, path a).

A CLI whose ``_srv()`` returns an instance of this transparently turns every
existing call site — ``_fn(s.task_ready)(repo=…)`` — into an authenticated MCP
``call_tool`` against the deployment. Nothing in the call sites changes.

Two shape details make that transparency work:

- FastMCP's ``CallToolResult.data`` already unwraps the ``{"result": …}``
  output-schema envelope back to the raw ``list``/``dict`` an in-process call
  returns, so the CLI's rendering code is untouched.
- CLI sites pass the first argument positionally (``s.task_get(slug)``); the MCP
  protocol is keyword-only. Positionals map to names using the tool's
  ``inputSchema`` property order, which FastMCP derives from the function
  signature (property order == signature order).

Server-specific policy is supplied by subclasses via the hooks below:
:meth:`~RemoteMCPProxy._is_admin_tool` / :meth:`~RemoteMCPProxy._admin_error`
(refuse in-process-only admin/break-glass tools),
:meth:`~RemoteMCPProxy._unknown_tool_error` (wording for a tool the deployment
doesn't expose), and :meth:`~RemoteMCPProxy._resolve_repo` (client-side repo
resolution, since the deployed server has no git checkout). Requires the
``remote`` extra (``fastmcp``).
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable

from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastmcp.client.transports import StreamableHttpTransport


class RemoteToolUnavailable(RuntimeError):
    """Raised when a CLI command has no remotely-callable counterpart."""


class RemoteMCPProxy:
    """Mirror a FastMCP server's tool surface, dispatching each call over MCP."""

    def __init__(self, url: str, token_provider: Callable[[], str]) -> None:
        self._url = url
        self._token_provider = token_provider
        self._param_names: dict[str, list[str]] | None = None
        self._lock = threading.Lock()

    # ── policy hooks (override in subclasses) ──────────────────────────────
    def _is_admin_tool(self, name: str) -> bool:
        """Whether ``name`` is an in-process-only admin tool to refuse remotely."""
        return False

    def _admin_error(self, name: str) -> str:
        """Message for an attempted call to an admin-only tool."""
        return f"`{name}` is not available over the remote CLI."

    def _unknown_tool_error(self, name: str) -> str:
        """Message when the deployment exposes no tool named ``name``."""
        return f"The deployed service exposes no `{name}` tool."

    def _resolve_repo(self) -> str | None:
        """Client-side value for ``repo=None`` (detect current repo). None: skip."""
        return None

    # ── dispatch ───────────────────────────────────────────────────────────
    def __getattr__(self, name: str) -> Callable[..., Any]:
        # Real instance attributes (_url, _token_provider, …) are set in
        # __init__ and never reach here; dunders must not be intercepted.
        if name.startswith("__"):
            raise AttributeError(name)
        if self._is_admin_tool(name):

            def _admin(*_args: Any, **_kwargs: Any) -> Any:
                raise RemoteToolUnavailable(self._admin_error(name))

            return _admin

        def _call(*args: Any, **kwargs: Any) -> Any:
            return asyncio.run(self._invoke(name, args, kwargs))

        return _call

    def _new_client(self, token: str) -> Client:
        """Build an MCP client authenticated with the caller's JWT.

        Isolated so tests can point the proxy at an in-memory FastMCP server.
        """
        transport = StreamableHttpTransport(self._url, auth=BearerAuth(token))
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
            raise RemoteToolUnavailable(self._unknown_tool_error(name))
        arguments = dict(kwargs)
        for i, val in enumerate(args):
            arguments[names[i]] = val
        # The deployed server has no git checkout, so it cannot resolve
        # repo=None ("detect current repo") — do that on the client and send an
        # explicit value. repo="" (all repos) is a meaningful sentinel, kept.
        if "repo" in names and arguments.get("repo") is None:
            detected = self._resolve_repo()
            if detected is not None:
                arguments["repo"] = detected
            else:
                arguments.pop("repo", None)
        # Drop remaining None values so omitted optionals take the tool's own
        # default rather than being sent as explicit nulls.
        return {k: v for k, v in arguments.items() if v is not None}
