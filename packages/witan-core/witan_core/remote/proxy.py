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
  protocol is keyword-only. Positionals map to names using the tool's input
  schema property order, which FastMCP derives from the function signature
  (property order == signature order). MCP SDK v2 renamed that field to
  ``input_schema``; ``_tool_input_schema`` reads whichever the installed
  fastmcp exposes, since the package supports both 3.4.x and 4.x. That list is
  held for as long as the server's own ``ttlMs`` says (MCP 2026-07-28), rather
  than for the process lifetime as it used to be.

Server-specific policy is supplied by subclasses via the hooks below:
:meth:`~RemoteMCPProxy._is_admin_tool` / :meth:`~RemoteMCPProxy._admin_error`
(refuse in-process-only admin/break-glass tools),
:meth:`~RemoteMCPProxy._unknown_tool_error` (wording for a tool the deployment
doesn't expose), :meth:`~RemoteMCPProxy._resolve_repo` (client-side repo
resolution, since the deployed server has no git checkout), and
:meth:`~RemoteMCPProxy._resolve_session_slug` (client-side workflow-session
handle, since a deployed replica shares no filesystem with the agent), and
:meth:`~RemoteMCPProxy._elicitation_handler` (who answers a prompt the
deployment raises — a terminal by default). Requires the ``remote`` extra
(``fastmcp``).
"""

from __future__ import annotations

import asyncio
import math
import sys
import threading
import time
from typing import Any, Callable

from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastmcp.client.elicitation import ElicitResult
from fastmcp.client.transports import StreamableHttpTransport


class RemoteToolUnavailable(RuntimeError):
    """Raised when a CLI command has no remotely-callable counterpart."""


async def console_elicitation_handler(
    message: str, response_type: type | None, params: Any, _ctx: Any
) -> ElicitResult:
    """Answer a deployed server's elicitation prompt from the terminal.

    A person is sitting at the CLI, so an ask can simply be put to them —
    unlike an agent-hosted client, which may accept the elicitation capability
    with no UI to render on. Reading stdin runs off-thread so the event loop
    driving the MCP connection keeps servicing it while the human types.

    A blank answer, EOF (Ctrl-D), or an interrupt (Ctrl-C) is a decline, which
    every witan call site maps onto the same default it uses for a client that
    can't elicit at all. Not a stdin-backed terminal (piped input, a cron run)
    declines without prompting rather than consuming the pipe.
    """
    if not sys.stdin or not sys.stdin.isatty():
        return ElicitResult(action="decline")
    schema = getattr(params, "requested_schema", None) or {}
    field = (schema.get("properties") or {}).get("value") or {}
    boolean = field.get("type") == "boolean"
    prompt = f"\n{message}\n{'[y/N]' if boolean else 'Answer'}: "
    try:
        answer = (await asyncio.to_thread(input, prompt)).strip()
    except (EOFError, KeyboardInterrupt):
        return ElicitResult(action="decline")
    if not answer:
        return ElicitResult(action="decline")
    if boolean:
        return ElicitResult(
            action="accept", content={"value": answer.lower() in ("y", "yes")}
        )
    return ElicitResult(action="accept", content={"value": answer})


def _tool_input_schema(tool: Any) -> dict:
    """A listed tool's input schema, across the MCP SDK v1→v2 field rename.

    fastmcp 3.4.x exposes ``inputSchema``; SDK v2 (fastmcp 4.x) renamed it to
    ``input_schema`` and warns on the old spelling. Both are supported versions
    of this package, so read the new name and fall back.
    """
    schema = getattr(tool, "input_schema", None)
    return schema if schema is not None else tool.inputSchema


_MISSING = object()


def _next_cursor(result: Any) -> str | None:
    """A list result's pagination cursor, across the same v1→v2 rename.

    Unlike the input schema, ``None`` is the *normal* value here — it means the
    last page — so presence has to be tested rather than truthiness. Reading the
    camelCase alias on a v2 result emits a deprecation warning, and this is on
    the path of every single tool call.
    """
    cursor = getattr(result, "next_cursor", _MISSING)
    return result.nextCursor if cursor is _MISSING else cursor


class RemoteMCPProxy:
    """Mirror a FastMCP server's tool surface, dispatching each call over MCP."""

    def __init__(self, url: str, token_provider: Callable[[], str]) -> None:
        self._url = url
        self._token_provider = token_provider
        self._param_names: dict[str, list[str]] | None = None
        # When the cached tool list goes stale, per the server's own ttlMs.
        # `inf` is the pre-2026-07-28 behavior — hold it for the process
        # lifetime — kept for servers that declare nothing to honor.
        self._param_names_expiry = math.inf
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

    def _resolve_session_slug(self) -> str | None:
        """Client-side value for an omitted ``session_slug``. None: skip.

        The ambient workflow-session handle, held by whichever process is
        client-side. MCP 2026-07-28 drops protocol-level session state, so a
        deployed replica cannot infer which agent session is calling — the
        handle has to travel as a tool argument for provenance edges to land.
        """
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

    def _elicitation_handler(self) -> Callable | None:
        """Handler answering the deployment's elicitation prompts. None: refuse.

        Advertising the capability is what makes a deployed tool ask at all: the
        2026-07-28 server checks for it before returning an ``input_required``
        result, and degrades to its own default when it is absent.
        """
        return console_elicitation_handler

    def _new_client(self, token: str) -> Client:
        """Build an MCP client authenticated with the caller's JWT.

        Isolated so tests can point the proxy at an in-memory FastMCP server.
        """
        transport = StreamableHttpTransport(self._url, auth=BearerAuth(token))
        return Client(transport, elicitation_handler=self._elicitation_handler())

    async def _refresh_param_names(self, client: Client) -> None:
        """Re-read the deployment's tool surface and its cache directive.

        Paginates through ``list_tools_mcp`` rather than calling ``list_tools``
        because only the protocol-level result carries ``ttlMs`` — the whole
        point being to hold the list for as long as the *server* says to,
        instead of guessing.

        A connection that declared no ``ttlMs`` has nothing to honor, so the
        expiry stays at ``inf`` and the list is held for the process lifetime as
        it always was. That has to be read off the *wire*, not the value: the
        field only exists from 2026-07-28, but the SDK model carries it with a
        default of 0 regardless of era, so a handshake-era peer would otherwise
        look like it had asked for no caching at all. A ``ttlMs`` of 0 the peer
        genuinely sent is different — a server declining to be cached — and
        re-listing each call is the honest reading of that.

        Across a paginated list the shortest declared TTL wins, and one page
        declaring is enough. FastMCP applies its hint uniformly so every page
        agrees in practice, but reading only the last page would let a final
        page that declared nothing silently upgrade an earlier "cache me for
        5 minutes" into "cache me forever" — the one direction that is unsafe.
        """
        names: dict[str, list[str]] = {}
        cursor: str | None = None
        declared_ttls: list[float] = []
        while True:
            page = await client.list_tools_mcp(cursor=cursor)
            for tool in page.tools:
                schema = _tool_input_schema(tool)
                names[tool.name] = list(schema.get("properties", {}).keys())
            if "ttl_ms" in getattr(page, "model_fields_set", ()):
                declared_ttls.append(page.ttl_ms)
            cursor = _next_cursor(page)
            if not cursor:
                break
        with self._lock:
            self._param_names = names
            self._param_names_expiry = (
                time.monotonic() + min(declared_ttls) / 1000
                if declared_ttls
                else math.inf
            )

    async def _invoke(self, name: str, args: tuple, kwargs: dict) -> Any:
        token = self._token_provider()
        async with self._new_client(token) as client:
            if (
                self._param_names is None
                or time.monotonic() >= self._param_names_expiry
            ):
                await self._refresh_param_names(client)
            arguments = self._map_args(name, args, kwargs)
            result = await client.call_tool(name, arguments)
            return result.data

    def _map_args(self, name: str, args: tuple, kwargs: dict) -> dict:
        assert self._param_names is not None
        names = self._param_names.get(name)
        if names is None:
            raise RemoteToolUnavailable(self._unknown_tool_error(name))
        if len(args) > len(names):
            # More positionals than the deployed tool accepts — a client/server
            # signature mismatch. Surface it clearly instead of an IndexError.
            raise RemoteToolUnavailable(
                f"`{name}` was called with {len(args)} positional argument(s) but "
                f"the deployed tool accepts {len(names)} ({', '.join(names)})."
            )
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
        # Same injection for the workflow-session handle. Under MCP 2026-07-28
        # the protocol carries no session state, so a deployed replica has no way
        # to tell which agent session is calling — the client supplies the handle
        # it persisted, and provenance edges land as they do under local stdio.
        # Applies to any tool declaring the parameter; where it is *required*
        # (``workflow_session_end``) callers already pass it explicitly, so this
        # only ever fills an omission.
        if "session_slug" in names and arguments.get("session_slug") is None:
            active = self._resolve_session_slug()
            if active is not None:
                arguments["session_slug"] = active
            else:
                arguments.pop("session_slug", None)
        # Drop remaining None values so omitted optionals take the tool's own
        # default rather than being sent as explicit nulls.
        return {k: v for k, v in arguments.items() if v is not None}
