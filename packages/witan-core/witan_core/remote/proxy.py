"""A drop-in stand-in for an in-process server module that dispatches each tool
call over MCP to a deployed FastMCP service (ADR-0005, path a).

A CLI whose ``_srv()`` returns an instance of this turns every existing call
site — ``_fn(s.task_ready)(repo=…)`` — into an authenticated MCP ``call_tool``
against the deployment.

Two shape details make that work:

- FastMCP's ``CallToolResult.data`` already unwraps the ``{"result": …}``
  output-schema envelope back to the raw ``list``/``dict`` an in-process call
  returns, so the CLI's rendering code is untouched.
- **Every argument must be passed by keyword.** The MCP protocol carries a
  name→value object; positional order is a property of a Python signature and
  does not survive serialisation. Callers that pass positionally get a
  :class:`RemoteToolUnavailable` naming the parameters, rather than a guess.

  This used to map positionals onto the tool's input-schema property order, on
  the stated assumption that "property order == signature order". JSON Schema
  ``properties`` is an unordered map *by specification*, so that was never a
  contract — and the two servers this proxy talks to really do disagree: an
  in-memory FastMCP publishes signature order, while the deployed tier
  publishes alphabetically. That divergence is why the assumption held in every
  local test and failed in production. Measured against the deployment, 29 of
  41 tools bound their first positional to the wrong parameter —
  ``memory_search(query)`` arrived as ``include_superseded``. A misbound
  parameter of a different type surfaces as a validation error, but two
  same-typed ``str`` parameters swap in silence:
  ``workflow_project_block(slug, blocks_slug)`` wrote its edge backwards. See
  ``_map_args``.

The tool list is held for as long as the server's own ``ttlMs`` says
(MCP 2026-07-28), rather than for the process lifetime as it used to be. MCP
SDK v2 renamed the schema field to ``input_schema``; ``_tool_input_schema``
reads whichever the installed fastmcp exposes, since the package supports both
3.4.x and 4.x.

Server-specific policy is supplied by subclasses via the hooks below:
:meth:`~RemoteMCPProxy._is_admin_tool` / :meth:`~RemoteMCPProxy._admin_error`
(refuse in-process-only admin/break-glass tools),
:meth:`~RemoteMCPProxy._unknown_tool_error` (wording for a tool the deployment
doesn't expose), :meth:`~RemoteMCPProxy._unreachable_hint` (what to check, and
how to opt into working locally, when the deployment cannot be reached),
:meth:`~RemoteMCPProxy._resolve_repo` (client-side repo
resolution, since the deployed server has no git checkout), and
:meth:`~RemoteMCPProxy._resolve_session_slug` (client-side workflow-session
handle, since a deployed replica shares no filesystem with the agent),
:meth:`~RemoteMCPProxy._resolve_session_id` (client-side agent-session id, for
the same reason), and
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
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Callable

import httpx2
from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastmcp.client.elicitation import ElicitResult
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError


class RemoteToolUnavailable(RuntimeError):
    """Raised when a CLI command has no remotely-callable counterpart."""


class RemotePayloadTooLarge(RuntimeError):
    """The deployment refused a request body as over its size cap (HTTP 413).

    Separate from :class:`RemoteUnreachable` because the deployment answered —
    it read the request far enough to reject it — and separate from a plain
    tool failure because nothing about the *call* was wrong, only its size.
    Conflating it with either sends you somewhere useless: "could not be
    reached" starts a hunt for a down endpoint that is in fact up, and a raw
    tool error says nothing about what to shrink.

    Never a signal to retry. The payload is what was refused, so sending it
    again gets the same answer for the same reason, having spent the same
    megabytes.
    """


class RemoteToolFailed(RuntimeError):
    """The deployment ran the tool and the tool refused — a Cedar denial, a
    missing slug, a schema mismatch, a bad argument.

    ★ EXISTS TO MAKE THE PROXY A REAL DROP-IN ★
    In-process, a tool that refuses raises ``RuntimeError`` and the CLI's
    ``except RuntimeError`` renders one red line. Over MCP the identical
    refusal comes back as ``fastmcp.exceptions.ToolError``, which is *not* a
    ``RuntimeError`` (``ToolError → FastMCPError → Exception``), so it sailed
    past every one of those handlers and the user got ~40 lines of cyclopts →
    asyncio → fastmcp internals with the real message on the last one. The
    message was always there; nothing rendered it. Subclassing ``RuntimeError``
    is what makes the same handler cover both tiers, which is the property the
    proxy's module docstring claims ("the CLI's rendering code is untouched").

    This does not walk back :meth:`RemoteMCPProxy._reclassifying`'s rule that a
    server-side raise "must keep its own error". That rule is about not
    relabelling a refusal as :class:`RemoteUnreachable` — about *where* the
    fault was, not which class carries it. A refusal still arrives as its own
    distinct type, still says what the server said, and still keeps the
    original ``ToolError`` as its ``__cause__`` for anyone who needs the wire
    form.
    """


class RemoteCredentialRejected(RuntimeError):
    """The deployment reached, and refused the credential.

    Raised in three situations, which the MESSAGE distinguishes because the
    reader's next step differs and only one of them is "log in again":

    * the refresh ran and the deployment still refused — re-authenticate;
    * this client holds a pinned credential and cannot refresh at all;
    * the call WRITES, so no refresh was attempted on purpose. The request had
      already reached the service, so its outcome is indeterminate and a retry
      could apply it twice; re-read before retrying.

    An earlier revision asserted here that the refresh had always been tried
    first. That was false for the write case and told the reader the recovery
    was exhausted when it had never begun.

    Its own type rather than :class:`RemoteUnreachable` because both of that
    class's implications are wrong here: the service WAS reached (it answered
    401/403), and the remedy is a credential, not an endpoint. Routing this
    through "could not be reached … check the endpoint" sent at least one
    investigation after DNS and ingress for an expired token — see
    tk-the-oidc-refresh-skew-30s-is-shorter-than-a-writ-8b04db.
    """


class RemoteWriteIndeterminate(RuntimeError):
    """A gateway cut the call off after it had been dispatched, and the tool WRITES.

    ★ NOT A FAILURE, AND NOT A SUCCESS. ★ Measured live against the CI
    deployment on 2026-08-12, counted from the rows afterwards rather than
    inferred: across two 16-writer bursts, one committed every row it 502'd on
    (28 attempted, 28 present) and the other did not (16 attempted, 14 present).
    So the deadline cuts the RESPONSE, the backend usually finishes the write
    anyway, and nothing in the reply says which happened this time.

    Distinct from :class:`RemoteUnreachable` because both of that class's
    implications are false here. The service WAS reached — it answered, through
    a proxy, having already started the work — and the operation is NOT safe to
    repeat: a blind retry writes the row twice whenever the first one landed,
    and `memory_store` with a generated slug has no key that would collapse the
    duplicate. Reporting this as "could not be reached" invites exactly that
    retry, which is why it is its own type with its own sentence.

    A read cut the same way stays :class:`RemoteUnreachable`: no answer came
    back, nothing changed server-side, and repeating it is free.

    ★ A RETRY IS SAFE IF — AND ONLY IF — THE ORIGINAL CARRIED AN
    ``idempotency_key``. ``memory_store`` and ``task_create`` derive the slug's
    suffix from that key, so a retry reusing it addresses the SAME slug; the
    server finds that row, returns it as ``idempotent_replay`` and writes
    nothing. The duplicate this class warns about is then structurally
    impossible.

    Note the mechanism is a skip, NOT an upsert, and the difference is the
    edges. Letting `insert` upsert the node would fix only the node: the
    ``Tagged``, ``SessionProduced``, ``ParentOf``, ``Blocks``,
    ``TaskBelongsTo`` and ``DiscoveredFrom`` writes beside it are unconditional
    inserts, and three keyed attempts were measured leaving one memory with
    three copies of each tag edge.

    ★ AND THIS IS SAID IN THE MESSAGE, NOT ONLY HERE. Callers see ``str(exc)``,
    so :meth:`RemoteMCPProxy._indeterminate_error` takes a ``keyed`` flag and
    tells a keyed caller to retry rather than to re-read. A docstring-only
    contract left the feature looking useless from the seat that needed it.

    Without a key the warning above stands unchanged: the suffix is random per
    attempt, so a retry writes a second row whenever the first one landed.
    Generate the key BEFORE the first attempt — one cannot be invented
    afterwards, because by then the slug the server chose is unknowable.

    This does not make the outcome KNOWN, only convergent. Learning whether the
    first attempt landed needs the write receipts omnigraph added after 0.9.0
    (upstream #479), which the deployment does not yet run.
    """


class RemoteUnreachable(RuntimeError):
    """The deployment could not be reached, or dropped mid-call.

    Distinct from :class:`RemoteToolUnavailable` on purpose, the same way
    witan-code separates ``ClusterUnreachable`` from ``ClusterGraphMissing``:
    "the service answered and has no such tool" and "I could not ask the
    service at all" are the same failed call from the caller's seat but send
    you to completely different places.

    Deliberately *not* a signal to fall back to a local store. Hard-failing is
    the documented behaviour (``docs/deployed-witan-onboarding.md``) — a silent
    fallback would split the corpus across two graphs with no signal that it
    happened, leaving a merge nobody knew to run. This exception exists so that
    refusal reads as a sentence instead of a traceback.
    """


_TRANSPORT_ERRORS = (httpx2.HTTPError, OSError)

# What a hop on this path says when it refuses a body for its size. Matched on
# the phrase rather than on a bare "413" because a tool error relays the
# server's own text, which can quote the caller's data — and every hop that can
# refuse us announces itself in words:
#   "request body too large"   the MCP Python SDK's RequestBodyLimitMiddleware
#   "request entity too large" ToolHive's pkg/bodylimit, and httpx's 413 reason
#   "payload too large"        omnigraph-server's axum DefaultBodyLimit
_TOO_LARGE_PHRASES = (
    "request body too large",
    "request entity too large",
    "payload too large",
)


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """Every exception reachable from ``exc``, through groups and causes.

    A fault raised deep in the client does not arrive as itself: it comes
    chained through fastmcp/anyio, and anyio's task groups re-raise inside an
    ``ExceptionGroup``. Walking the group members *and* the cause chain is what
    keeps "the pod restarted during my write" from reading as a tool error.
    ``seen`` guards the cycles ``__context__`` can form.
    """
    seen: set[int] = set()

    def walk(err: BaseException | None) -> Iterator[BaseException]:
        if err is None or id(err) in seen:
            return
        seen.add(id(err))
        yield err
        if isinstance(err, BaseExceptionGroup):
            for member in err.exceptions:
                yield from walk(member)
        yield from walk(err.__cause__)
        yield from walk(err.__context__)

    return walk(exc)


def _transport_failure(exc: BaseException) -> BaseException | None:
    """The transport-level error inside ``exc``, or None if there is none.

    A connection that fails while opening surfaces as fastmcp's own
    ``RuntimeError("Client failed to connect: …")``, so the connect phase is
    classified by *where* it failed rather than by type. A connection that
    drops mid-call has no such wrapper, which is what :func:`_chain` is for.
    """
    return next((e for e in _chain(exc) if isinstance(e, _TRANSPORT_ERRORS)), None)


def payload_too_large(exc: BaseException) -> BaseException | None:
    """The "body over the size cap" error inside ``exc``, or None.

    ★ MUST BE ASKED BEFORE :func:`_transport_failure`. A 413 that arrives from
    a direct connection is an ``httpx2.HTTPStatusError``, which *is* an
    ``httpx2.HTTPError`` — so classifying by transport first reports a
    deployment that is plainly up and answering as one that "could not be
    reached", and sends the reader to check an endpoint that is fine.

    Two shapes reach us, because two different hops can do the refusing. On a
    direct connection the status code is on the response. Through ToolHive's
    vMCP the refusal happened on *its* upstream call, so it comes back as a
    perfectly successful HTTP exchange carrying a tool error whose text quotes
    the 413 — no status code anywhere, only the words.

    Public because witan-code's store session holds its own connection and so
    does its own classification, and one definition of "this was refused for
    its size" is what keeps the two transports agreeing.
    """
    for err in _chain(exc):
        response = getattr(err, "response", None)
        if getattr(response, "status_code", None) == 413:
            return err
        text = str(err).lower()
        if any(phrase in text for phrase in _TOO_LARGE_PHRASES):
            return err
    return None


# Statuses an intervening proxy returns for "I reached the upstream and did not
# get a usable reply out of it". Both mean the request was DISPATCHED, which is
# the property that makes a write's outcome unknowable:
#   502  the upstream connection was torn down mid-response — what APISIX renders
#        when ToolHive's vMCP hits its own hardcoded 30s deadline and Go closes
#        the connection out from under the handler
#   504  the proxy gave up waiting for the upstream first
#
# 503 is deliberately absent: it means no upstream was available to try, so
# nothing was dispatched and the existing "unreachable, safe to retry" reading is
# the correct one. That distinction is the whole basis for this list.
_GATEWAY_STATUSES = frozenset({502, 504})

# The deployment rejected the credential. Distinct from every other failure
# here because it is the only one the CLIENT can fix mid-flight, by refreshing.
#
# 403 rides along with 401 deliberately: witan's server answers an unknown or
# unauthorised actor that way, and the remedy a user needs to be told is the
# same — the identity is wrong or stale, not the endpoint.
_AUTH_STATUSES = frozenset({401, 403})


def auth_failure(exc: BaseException) -> BaseException | None:
    """The credential rejection inside ``exc``, or None.

    ★ MUST BE ASKED BEFORE :func:`_transport_failure`, for the third time in
    this module and the same reason as :func:`payload_too_large` and
    :func:`gateway_failure`: it arrives as an ``httpx2.HTTPStatusError``, which
    IS an ``httpx2.HTTPError``, so classifying by transport first files an
    expired token under "the deployed service could not be reached … check the
    endpoint". That message sends somebody to inspect DNS and ingress over a
    token they could have refreshed.
    """
    for err in _chain(exc):
        status = getattr(getattr(err, "response", None), "status_code", None)
        if status in _AUTH_STATUSES:
            return err
    return None


def gateway_failure(exc: BaseException) -> BaseException | None:
    """The dispatched-then-cut-off error inside ``exc``, or None.

    ★ MUST BE ASKED BEFORE :func:`_transport_failure`, for the same reason
    :func:`payload_too_large` must be: these arrive as ``httpx2.HTTPStatusError``,
    which IS an ``httpx2.HTTPError``, so classifying by transport first buries
    every one of them under "could not be reached".
    """
    for err in _chain(exc):
        status = getattr(getattr(err, "response", None), "status_code", None)
        if status in _GATEWAY_STATUSES:
            return err
    return None


def tool_failure(exc: BaseException) -> BaseException | None:
    """The server-side tool refusal inside ``exc``, or None.

    ★ MUST BE ASKED LAST — AFTER :func:`payload_too_large` ★
    A 413 relayed by ToolHive's vMCP *is* a ``ToolError``: the HTTP exchange
    with vMCP succeeded, and the upstream refusal comes back as tool-error text
    quoting the limit. Asking this first would file every one of those under
    "the tool refused", losing the one classification that tells the caller to
    send less rather than to fix their call.

    Walks the chain rather than testing ``exc`` directly, for the same reason
    :func:`_transport_failure` does: anyio re-raises through an
    ``ExceptionGroup``, so the ``ToolError`` is not always the outermost thing.
    """
    return next((e for e in _chain(exc) if isinstance(e, ToolError)), None)


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

    def __init__(
        self,
        url: str,
        token_provider: Callable[[], str],
        token_refresher: Callable[[str], str] | None = None,
    ) -> None:
        """``token_refresher`` force-mints a credential, bypassing any cache.

        It takes the credential that was REJECTED, so it can tell "the cache
        still holds the dead one, refresh it" from "somebody already replaced
        it, use theirs" — re-reading the cache instead would refresh a fresh
        token under concurrent 401s and spend a rotating refresh token twice.

        Optional, and its absence is a supported configuration rather than a
        degraded one: the concurrency probe deliberately PINS one token across
        its workers (`lambda: token`) precisely so a fleet cannot stampede the
        shared cache, and a pinned token has nothing to refresh to. Without a
        refresher a 401 is reported rather than retried, which is the old
        behaviour made explicit.

        Distinct from ``token_provider`` because that one is allowed to answer
        from cache — and on a rejected token it necessarily WOULD, handing back
        the same dead credential and turning a retry into a second 401.
        """
        self._url = url
        self._token_provider = token_provider
        self._token_refresher = token_refresher
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

    def _unreachable_hint(self) -> str:
        """Why there is no fallback, and what to do instead, for THIS CLI.

        Server-specific on both halves. The setting to unset differs per CLI
        and per config shape (a named target vs. a bare env var), exactly as
        witan-code's ``_index_locally_hint`` does — and so does the *reason*
        the client refuses to serve a local answer, since witan's stores hold a
        corpus that would silently fork while witan-code's hold a cache that
        would silently go stale.
        """
        return "There is no local fallback. Check that the endpoint is up."

    def _unreachable_error(self, exc: BaseException) -> str:
        """Message for a deployment that could not be reached.

        The no-fallback rule belongs in the message, not just the docs: the
        failure this replaces is a user assuming their commands quietly went to
        the local store and only discovering otherwise at merge time.
        """
        return (
            f"The deployed service at {self._url} could not be reached: "
            f"{exc}. {self._unreachable_hint()}"
        )

    def _writes(self, name: str) -> bool:
        """Whether calling ``name`` can change the graph. Default: assume it can.

        ★ THE DEFAULT IS DELIBERATELY THE CAUTIOUS ONE. ★ This is only ever
        consulted about a call whose outcome is already unknown, and the two
        possible mistakes are not symmetric: calling a read a write costs one
        over-careful sentence ("re-read before retrying" for a call that changed
        nothing), while calling a write a read tells someone their write did not
        happen when it may well have. A subclass that lists its read-only tools
        therefore lists exactly those, and anything it has not heard of —
        a tool added after the list was written — lands on the safe side.
        """
        return True

    def _indeterminate_error(self, name: str, status: int, *, keyed: bool) -> str:
        """Message for a write whose fate the gateway made unknowable.

        Names the ambiguity in the first sentence. An earlier version of this
        path said "could not be reached", which is wrong in both halves and
        wrong in the expensive direction: the service was reached, and the
        remedy it implies — try again — is what duplicates the row.

        ★ ``keyed`` CHANGES THE ADVICE, BECAUSE THE KEY CHANGES THE FACTS. The
        outcome is equally unknown either way — that is the gateway's doing and
        no client-side argument fixes it. What differs is the remedy. Without a
        key, retrying writes the row twice if the first attempt landed, so the
        only safe move is to re-read. With one, the server recognises the slug
        the key determines and returns the original row untouched, so the retry
        converges instead of duplicating and there is nothing to check first.

        Telling a keyed caller to re-read anyway is not merely noise: it is the
        advice that made this feature look useless, since the whole point of
        supplying the key was to make the retry safe.

        The STATUS is quoted rather than the exception, whose ``str`` is
        httpx's three-line "for more information check…" paragraph. The number
        is the whole content; the rest is chained on ``__cause__`` for anyone
        who wants it.
        """
        if keyed:
            remedy = (
                "This call carried an idempotency_key, so RETRYING IT IS SAFE: "
                "reissue the identical call with the same key. If the write did "
                "land, the server returns that row and writes nothing further."
            )
        else:
            remedy = (
                "Re-read before retrying; retrying blind writes it twice if it "
                "did land. Passing an idempotency_key would make the retry safe."
            )
        return (
            f"The deployed service at {self._url} answered HTTP {status} for "
            f"`{name}`: the request reached it and was cut off before a reply "
            f"came back. `{name}` writes, so ITS OUTCOME IS INDETERMINATE — the "
            "write may or may not have been applied, and nothing in this "
            f"response says which. {remedy}"
        )

    def _credential_rejected_error(
        self, name: str, status: int, *, refreshed: bool
    ) -> str:
        """Message for a credential the deployment refused.

        ★ ``refreshed`` IS WHAT ACTUALLY HAPPENED, not what was configured.
        Holding a refresher does not mean one was used: a WRITE is re-raised
        without refreshing, deliberately, so reporting "a refresh was attempted
        and still refused" there tells the reader the recovery has been
        exhausted when it was never begun — and sends them to re-authenticate
        instead of re-reading the graph.
        """
        if refreshed:
            tried = "A refresh was attempted and the deployment still refused."
        elif self._token_refresher is None:
            tried = "This client holds a pinned credential and cannot refresh it."
        else:
            tried = (
                f"No refresh was attempted: `{name}` writes, and the request "
                "had already reached the service, so its outcome is "
                "INDETERMINATE — retrying could apply it twice. Re-read before "
                "retrying."
            )
        return (
            f"The deployed service at {self._url} answered HTTP {status} for "
            f"`{name}`: it was reached, and it rejected the credential. "
            f"{tried} The endpoint is fine — re-authenticate (`witan login`) "
            "rather than checking the URL."
        )

    def _gateway_read_error(self, name: str, status: int) -> str:
        """Message for a read cut off the same way. Reached, answered nothing."""
        return (
            f"The deployed service at {self._url} answered HTTP {status} for "
            f"`{name}`: the request reached it and was cut off before a reply "
            "came back. Nothing was read and nothing changed, so this is safe "
            "to retry — it usually means the service is saturated rather than "
            "down."
        )

    def _payload_too_large_error(self, name: str, exc: BaseException) -> str:
        """Message for a request body the deployment refused for its size.

        ★ DELIBERATELY OPERATION-NEUTRAL. This fires for EVERY tool call —
        a `memory_store` with a large body, a read, a single mutation — not
        only for the byte-chunked bulk writes. An earlier revision asserted
        here that "bulk writes are split into 2 MiB batches" and that "batches
        before this one were applied, the write stopped part-way". Both are
        false off the merge path, and the second is false in the direction that
        does harm: it tells someone whose single call was refused that their
        graph is now half-mutated, when nothing was written at all.

        Callers that DO know they are mid-batch add that context themselves,
        where the batch number, the budget actually in play, and whether
        anything was applied are all real rather than assumed — see
        ``witan.remote.proxy._merge_batch_refusal``.
        """
        return (
            f"The deployed service at {self._url} refused `{name}`: the request "
            f"body is over its size cap ({exc}). Retrying sends the same bytes "
            "to the same answer — the payload itself is what was rejected, so "
            "it has to get smaller."
        )

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

    def _resolve_session_id(self) -> str | None:
        """Client-side value for an omitted ``session_id``. None: skip.

        The raw agent-session id, for tools that need to tell two sessions of
        one *person* apart rather than attribute a node to a session — the
        advisory task claim is the case. Same reason as ``session_slug``: the
        deployed replica has neither the environment variable nor any
        protocol-level session state, so the value has to travel as an
        argument. Distinct from ``session_slug`` because it needs no workflow
        session to exist first; every agent run has an id, only some have a
        project.
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

    @asynccontextmanager
    async def _reclassifying(
        self, name: str, *, refreshed: bool = False, keyed: bool = False
    ) -> AsyncIterator[None]:
        """Report a fault raised in this block as the sentence it deserves.

        By exception type, not by position: a tool that raises server-side is a
        legitimate answer and must keep its own error, so only a genuine
        transport fault (:func:`_transport_failure`) is the deployment going
        away. Errors this class already classified pass through untouched.

        The five questions are asked in a fixed order, and each ordering is
        load-bearing:

        1. Size FIRST — see :func:`payload_too_large` for why asking it second
           silently mislabels every direct-connection 413 as unreachable.
        2. Gateway cut-off next — see :func:`gateway_failure`. Same trap as the
           413: a 502 is an ``httpx2.HTTPError``, so asking about the transport
           first files a service that answered under "could not be reached" and,
           on a write, invites the retry that duplicates the row.
        3. Credential rejection — see :func:`auth_failure`. The same trap a
           third time, and the one with the most misleading fallout: an expired
           token reported as "check the endpoint" sends somebody after DNS and
           ingress for something a refresh fixes.
        4. Transport next, since a drop is the deployment going away.
        5. Tool refusal LAST — see :func:`tool_failure`. A vMCP-relayed 413
           arrives as a ``ToolError`` too, so this must not get first look.

        A refusal becomes :class:`RemoteToolFailed` rather than propagating as
        the raw ``ToolError``: it is still its own type carrying the server's
        own words, but it is now a ``RuntimeError``, which is what the CLI has
        always caught for the identical in-process failure. Anything this does
        not recognise still propagates untouched.
        """
        try:
            yield
        except (
            RemoteToolUnavailable,
            RemoteUnreachable,
            RemotePayloadTooLarge,
            RemoteWriteIndeterminate,
            RemoteCredentialRejected,
        ):
            raise
        except Exception as exc:  # noqa: BLE001 — re-raised unless classified
            oversized = payload_too_large(exc)
            if oversized is not None:
                raise RemotePayloadTooLarge(
                    self._payload_too_large_error(name, oversized)
                ) from exc
            cut_off = gateway_failure(exc)
            if cut_off is not None:
                status = cut_off.response.status_code
                if self._writes(name):
                    raise RemoteWriteIndeterminate(
                        self._indeterminate_error(name, status, keyed=keyed)
                    ) from exc
                raise RemoteUnreachable(self._gateway_read_error(name, status)) from exc
            rejected = auth_failure(exc)
            if rejected is not None:
                raise RemoteCredentialRejected(
                    self._credential_rejected_error(
                        name, rejected.response.status_code, refreshed=refreshed
                    )
                ) from exc
            dropped = _transport_failure(exc)
            if dropped is not None:
                raise RemoteUnreachable(self._unreachable_error(dropped)) from exc
            refused = tool_failure(exc)
            if refused is None:
                raise
            # Chained from the ToolError itself, not from `exc`. When anyio
            # re-raises through an ExceptionGroup the two differ, and `from exc`
            # would put the *group* on __cause__ — leaving a caller to re-walk
            # it for the wire error this class promises to hand over. The group
            # is not lost either way: it is the exception being handled, so it
            # lands on __context__.
            raise RemoteToolFailed(str(refused)) from refused

    async def _invoke(self, name: str, args: tuple, kwargs: dict) -> Any:
        """Dispatch one tool call, classifying an unreachable deployment.

        Opening the connection is its own step — via an exit stack rather than
        a plain ``async with`` — because *anything* that fails there is a
        transport failure by construction: a bad DNS name, a closed port, a TLS
        error, a 5xx from an ingress, a token the server rejects. fastmcp
        reports all of them as a bare ``RuntimeError``, so classifying by
        position is the only honest reading.

        Everything else — the call itself *and closing the connection* — is
        classified by type inside :meth:`_reclassifying`. Teardown matters as
        much as the call: ``AsyncExitStack.__aexit__`` re-raises what fastmcp's
        anyio background tasks failed with, so a drop noticed only while the
        client is closing would otherwise escape as exactly the traceback this
        exists to remove. It wraps the stack rather than sitting inside it for
        that reason. A non-transport cleanup error still propagates as itself.
        """
        # Bound before the attempt so the refresher below can be told exactly
        # which credential was refused, rather than re-reading a cache another
        # worker may already have replaced.
        token = self._token_provider()
        try:
            return await self._invoke_once(name, args, kwargs, token)
        except RemoteCredentialRejected:
            # ★ ONE RETRY, AND ONLY FOR A READ.
            #
            # The credential can go stale mid-session: tokens here live ~5
            # minutes and a single write has been measured at up to 51s, so a
            # run of slow calls crosses an expiry boundary as a matter of
            # course. That is recoverable, and making the user re-run the
            # command by hand is a worse answer than refreshing.
            #
            # But NOT for a write, and this is the same asymmetry the 502 path
            # settled: the request reached the server, which may have applied
            # it either side of the credential being judged. Retrying blind
            # writes the row twice whenever it did land, and no response
            # distinguishes those cases. `_writes` already encodes this, with
            # "assume it wrote" as the default for anything unclassified.
            #
            # A pinned-token client (no refresher) has nothing to retry WITH —
            # the provider would hand back the same dead token.
            if self._writes(name) or self._token_refresher is None:
                raise
            fresh = self._token_refresher(token)
            return await self._invoke_once(name, args, kwargs, fresh, refreshed=True)

    async def _invoke_once(
        self,
        name: str,
        args: tuple,
        kwargs: dict,
        token: str,
        *,
        refreshed: bool = False,
    ) -> Any:
        """One dispatch attempt with an already-resolved ``token``.

        ``refreshed`` records whether this attempt is the post-refresh retry,
        so a message can say what was actually tried rather than what was
        configured.
        """
        # Whether THIS invocation can be safely retried, which is a property of
        # the call's arguments and not of the tool. Read from kwargs only: these
        # tools are dispatched by keyword (`_invoke` forwards the caller's
        # kwargs verbatim), and guessing a positional index would be wrong the
        # first time a signature gained a parameter. A blank key is not a key —
        # the server rejects it outright, so it must not earn the safe wording.
        keyed = bool(str(kwargs.get("idempotency_key") or "").strip())
        async with (
            self._reclassifying(name, refreshed=refreshed, keyed=keyed),
            AsyncExitStack() as stack,
        ):
            try:
                client = await stack.enter_async_context(self._new_client(token))
            except Exception as exc:  # noqa: BLE001 — see docstring
                # A rejected credential is knowable here when the status
                # survives, and must not be swallowed by the blanket
                # transport reading — otherwise the refresh above never fires
                # for the case that motivated it.
                rejected = auth_failure(exc)
                if rejected is not None:
                    raise RemoteCredentialRejected(
                        self._credential_rejected_error(
                            name, rejected.response.status_code, refreshed=refreshed
                        )
                    ) from exc
                raise RemoteUnreachable(self._unreachable_error(exc)) from exc
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
        if args:
            # Positional arguments cannot be mapped onto an MCP call. The wire
            # format is a name->value object, and the only ordering information
            # available here is the input schema's `properties`, which is an
            # unordered map by JSON Schema specification, and servers disagree
            # on what they emit. Binding by that order silently sent
            # `memory_search(query)` as `include_superseded`, and swapped
            # same-typed pairs like `workflow_project_block(slug, blocks_slug)`
            # without any error at all. Refuse instead of guessing: a caller
            # that must be fixed should find out here, not in the graph.
            #
            # A tool can legitimately declare no parameters at all
            # (`code_indexed_repos`), where "Accepted names: ." would say
            # nothing — name the real problem instead.
            accepted = (
                f"Accepted names: {', '.join(names)}."
                if names
                else "This tool accepts no arguments."
            )
            raise RemoteToolUnavailable(
                f"`{name}` was called with {len(args)} positional argument(s). "
                "Remote tool calls must pass every argument by keyword — MCP "
                "carries arguments by name and the protocol defines no "
                f"parameter order. {accepted}"
            )
        arguments = dict(kwargs)
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
        # And the raw session id, for the advisory task claim. Reading it from
        # the *server* environment would have worked only under local stdio,
        # where the server is a child of the agent and inherits it; a deployed
        # pod has no such variable, so every remote caller would collapse back
        # onto the bare identity — precisely the case the claim qualifier
        # exists for, since concurrent users are what a shared deployment is.
        if "session_id" in names and arguments.get("session_id") is None:
            sid = self._resolve_session_id()
            if sid is not None:
                arguments["session_id"] = sid
            else:
                arguments.pop("session_id", None)
        # Drop remaining None values so omitted optionals take the tool's own
        # default rather than being sent as explicit nulls.
        return {k: v for k, v in arguments.items() if v is not None}
