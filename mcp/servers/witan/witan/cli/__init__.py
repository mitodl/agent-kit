"""The ``witan`` umbrella CLI.

Exposed as ``witan`` (see pyproject ``[project.scripts]``). Covers the
work-coordination graph (tasks, workflow projects, memory), starts the MCP
server (``witan serve``), and — when ``witan-code`` is installed — mounts the
code-graph tool as ``witan code …``.

It is a thin presentation layer: every query goes through the same
``witan.server`` tool functions the MCP server exposes, so behaviour
(repo scoping, ready-work computation, …) stays identical.
"""

from __future__ import annotations

from typing import Annotated, Literal

import cyclopts
from rich.markup import escape

from .. import config as cfg_module

# Import submodules to trigger @app.command / @*_app.command registrations.
from . import (
    auth,  # noqa: F401
    graph,  # noqa: F401
    hooks,  # noqa: F401
    maintenance,  # noqa: F401
    memory,  # noqa: F401
    projects,  # noqa: F401
    scan,  # noqa: F401
    session,  # noqa: F401
    setup_cmd,  # noqa: F401
    targets,  # noqa: F401
    tasks,  # noqa: F401
    traces,  # noqa: F401
)
from ._common import app, console, print_error, stderr_console
from .migrate import migrate_app
from .output import OutputFormat, set_output_format
from .run_helpers import _run_task_slug

# ── How long a shutting-down server waits for work already in flight ─────────
#
# ★ FASTMCP'S OWN DEFAULT IS 2 SECONDS, AND THAT SILENTLY TRUNCATES A ROLLOUT.
# `FastMCP.run_http_async` builds its uvicorn config with a hardcoded
# `timeout_graceful_shutdown: 2` (fastmcp 4.0.0b2), so on SIGTERM uvicorn stops
# accepting connections, gives in-flight requests two seconds, and drops the
# rest. A witan write has been measured at 27s under load, so every one of them
# is severed by a deploy, an eviction or a node drain — and a severed write is
# the indeterminate outcome the caller cannot safely retry, which is the exact
# failure class this deployment has spent weeks removing elsewhere.
#
# It also cannot be fixed from the deployment side alone. ol-infrastructure sets
# `terminationGracePeriodSeconds: 150` so the kubelet will wait; uvicorn declines
# to use it. Both halves are required, and the pod-side one is the half that
# looks sufficient — a comment there asserted exactly that until this was found.
#
# 120s matches the request budget the deployment enforces at APISIX
# (`WITAN_REQUEST_TIMEOUT`), on the principle that shutdown should be willing to
# wait as long as a request was allowed to take. Anything already past that
# budget is being cut off upstream anyway. Local stdio runs never reach this
# code, and a local HTTP run has nothing in flight worth waiting for, so the
# default is safe everywhere rather than only in the cluster.
DEFAULT_SHUTDOWN_GRACE_SECONDS = 120.0

# Mount `witan migrate …` (sub-app, not a flat command).
app.command(migrate_app, name="migrate")

# Mount the code-graph CLI as `witan code …` when witan-code is installed.
# Optional: the umbrella works standalone without it.
try:
    from witan_code.cli import app as _code_app

    app.command(_code_app, name="code")
except ImportError:
    pass


def _local_code_graph_warning(transport: str, target_name: str | None) -> str:
    """The warning text, separated from the config read so it can be tested.

    Everything interpolated here is escaped, because the target name lands
    inside brackets: "target [production]" is valid Rich markup for a style
    called `production`, so unescaped the name is either swallowed on render or
    raises on a name Rich cannot parse. Splitting this out also keeps the
    escaping under test where `witan-code` is not installed — otherwise the
    only test of it skips, which is no test at all.
    """
    where = f"target [{target_name}]" if target_name else "config"
    return (
        f"[yellow]witan: memory graph is deployed, code graphs are local "
        f"(code_transport = {escape(repr(transport))}). Branches indexed here "
        f'stay on this machine. Set code_transport = "mcp" on {escape(where)} '
        f"to share them.[/yellow]"
    )


def _warn_if_code_graph_is_local() -> None:
    """Warn when the memory graph is deployed but code graphs are not.

    The two are routed by SEPARATE settings — ``remote_url`` sends the
    work/memory graph to the deployment, ``code_transport = "mcp"`` sends the
    code graphs — and nothing ties them together. A target that sets the first
    and not the second gives you an agent whose memory is shared and whose code
    graph is a directory on one laptop, which defeats the point of indexing a
    branch at all: the reason branches are indexed per writer is so another
    session, and another developer, can see work that is still in flight.

    A warning rather than a refusal. Unlike the memory-graph fallback this
    module exists to stop, this one is legible from the outside — `witan code`
    reports the store path it used — and a local code graph is a legitimate
    choice for someone who has not provisioned cluster graphs yet.
    """
    try:
        from witan_code import config as code_cfg_module
    except ImportError:
        return
    code_cfg = code_cfg_module.load()
    if code_cfg.code_transport == code_cfg_module.CODE_TRANSPORT_MCP:
        return
    stderr_console.print(
        _local_code_graph_warning(code_cfg.code_transport, code_cfg.target_name)
    )


def _serve_target(transport: str):
    """The FastMCP server to run: the deployment's surface, or the local store.

    Resolved the same way ``witan.cli._common._srv`` resolves the CLI's tool
    provider, which is the entire point — before this, ``serve`` was the one
    caller that ignored ``remote_url`` and opened the local store instead,
    silently splitting an agent's writes from its operator's commands.

    Refuses to start rather than falling back. An unreachable deployment or an
    expired session is a reason to stop and say so; it is not a reason to write
    somewhere else and let a merge nobody knew to run accumulate.

    ★ REMOTE RE-SERVING IS STDIO-ONLY, AND THAT IS A SECURITY BOUNDARY RATHER
    THAN A LIMITATION. Every forwarded call is authenticated with the cached
    OIDC token of the user who started the process, and this server has no
    inbound authentication of its own. Over stdio that is exactly right — the
    only client is the agent harness that spawned it, as that same user. Bound
    to a socket it becomes a credential-sharing proxy: anyone who can reach the
    listener acts as the token's owner, with none of the per-caller JWT->actor
    mapping (ADR-0004) the real deployment does. `--host 0.0.0.0` is documented
    on this command, so this is reachable by configuration, not just by
    mistake.

    The deployment's own `--transport streamable-http` is unaffected: it serves
    its own graph and has no `remote_url`, so it never reaches this branch.
    """
    import asyncio

    from .remote_errors import remote_serving_needs_stdio, remote_startup_failure

    try:
        remote = cfg_module.load_remote_config()
    except ValueError as exc:
        print_error(exc, stderr=True)
        raise SystemExit(1) from None

    if remote is None:
        from ..server import mcp as witan_mcp

        return witan_mcp

    if transport != "stdio":
        print_error(remote_serving_needs_stdio(remote, transport), stderr=True)
        raise SystemExit(1) from None

    from ..remote.serve import build_remote_server

    try:
        server = asyncio.run(build_remote_server(remote))
    except Exception as exc:  # noqa: BLE001 — every failure mode is the same answer
        print_error(remote_startup_failure(remote, exc), stderr=True)
        raise SystemExit(1) from None
    _warn_if_code_graph_is_local()
    return server


@app.command
def serve(
    *,
    transport: Annotated[
        Literal["stdio", "http", "streamable-http"],
        cyclopts.Parameter(env_var="WITAN_MCP_TRANSPORT"),
    ] = "stdio",
    host: Annotated[str, cyclopts.Parameter(env_var="WITAN_MCP_HOST")] = "127.0.0.1",
    port: Annotated[int, cyclopts.Parameter(env_var="WITAN_MCP_PORT")] = 8000,
    path: Annotated[str, cyclopts.Parameter(env_var="WITAN_MCP_PATH")] = "/mcp",
    shutdown_grace_seconds: Annotated[
        float, cyclopts.Parameter(env_var="WITAN_MCP_SHUTDOWN_GRACE_SECONDS")
    ] = DEFAULT_SHUTDOWN_GRACE_SECONDS,
) -> None:
    """Run the witan MCP server.

    Serves the work-coordination tools (memory_*, task_*, workflow_*) and, when
    witan-code is installed, mounts the code-graph tools (code_*) into the same
    server so a single MCP entry exposes everything.

    Defaults to ``stdio`` for local per-user use (Claude Desktop, ``uvx``). Pass
    ``--transport streamable-http`` (or set ``WITAN_MCP_TRANSPORT``) to expose an
    HTTP endpoint for a shared, deployed service — this is what ToolHive hosts.

    The legacy HTTP+SSE transport is not offered: MCP 2026-07-28 deprecates it
    with a 12-month offramp, and witan has no deployment on it to carry over.

    Parameters
    ----------
    transport: MCP transport. ``stdio`` for local; ``streamable-http`` (or its
        ``http`` alias) binds a network listener. Env: ``WITAN_MCP_TRANSPORT``.
    host: Interface to bind for HTTP transports. ``0.0.0.0`` inside a container.
        Env: ``WITAN_MCP_HOST``.
    port: Port to bind for HTTP transports. Env: ``WITAN_MCP_PORT``.
    shutdown_grace_seconds: How long uvicorn waits for in-flight requests after
        SIGTERM before dropping them. FastMCP's own default is **2 seconds**,
        which silently truncates any deployment that expects a rollout to drain
        — a witan write has been measured at 27s. Set this to the deployment's
        termination grace period. Env:
        ``WITAN_MCP_SHUTDOWN_GRACE_SECONDS``.
    path: URL path the MCP endpoint is served on (HTTP transports only).
        Env: ``WITAN_MCP_PATH``.
    """
    # Before the server import, so anything logged while the module initialises
    # (auth wiring, store resolution) lands in the configured pipeline rather
    # than on an unconfigured root logger.
    from witan_core.observability import configure_observability

    configure_observability()

    witan_mcp = _serve_target(transport)

    try:
        from witan_code.server import mcp as code_mcp

        witan_mcp.mount(code_mcp)
    except ImportError:
        pass

    if transport == "stdio":
        witan_mcp.run()
    else:
        # Starlette routing asserts a leading slash; be forgiving of `mcp`.
        if not path.startswith("/"):
            path = f"/{path}"
        # ASGI, not FastMCP, middleware: FastMCP builds its span at the protocol
        # layer BEFORE its own middleware chain runs, so the caller's context
        # has to be attached further out or the span is already a rival root.
        # This is what joins witan's spans to ToolHive's trace — ToolHive
        # propagates over HTTP headers, which nothing else here reads. See
        # `witan_core.observability.asgi`.
        from witan_core.observability import trace_context_middleware

        witan_mcp.run(
            transport=transport,
            host=host,
            port=port,
            path=path,
            middleware=trace_context_middleware(),
            # Overrides FastMCP's hardcoded 2s. See
            # DEFAULT_SHUTDOWN_GRACE_SECONDS — without this the deployment's
            # 150s termination grace buys time uvicorn refuses to use, and every
            # in-flight write is severed by a rollout.
            uvicorn_config={"timeout_graceful_shutdown": shutdown_grace_seconds},
        )


@app.command
def run(
    slug: str,
    *,
    target: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    claim: bool = True,
    dry_run: bool = False,
) -> None:
    """Claim a task and launch an agent to execute it.

    Claims the task (status in_progress, assignee = your author), then hands the
    terminal to ``<agent>`` seeded with a prompt describing the work. Run from
    the task's repo checkout so the agent has the right working directory.

    Parameters
    ----------
    target: Named config target to use (overrides auto-detection by repo org).
        Also overridable via WITAN_TARGET env var.
    agent: Agent CLI to launch (claude, pi, copilot, opencode, kilo). Overrides
        WITAN_AGENT env var and target/config-file default.
    model: Model passed to the agent's --model flag. Overrides WITAN_MODEL env
        var and target/config-file default.
    claim: Mark the task in_progress and assign it to you first.
    dry_run: Print the prompt and exit without launching or claiming.
    """
    try:
        cfg = cfg_module.load(target=target)
    except ValueError as exc:
        print_error(exc)
        raise SystemExit(1) from None
    _run_task_slug(
        slug, cfg=cfg, agent=agent, model=model, claim=claim, dry_run=dry_run
    )


@app.meta.default
def _launcher(
    *tokens: Annotated[str, cyclopts.Parameter(show=False, allow_leading_hyphen=True)],
    output_format: Annotated[
        OutputFormat,
        cyclopts.Parameter(name="--output-format", env_var="WITAN_OUTPUT_FORMAT"),
    ] = "txt",
) -> None:
    """witan — agent memory, planning, and collaboration graph.

    Parameters
    ----------
    output_format: Output format for table commands. Commands include tasks,
        projects, memory, traces, scan, and mounted witan-code tables. Values:
        txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT.
    """
    set_output_format(output_format)
    try:
        from witan_code.output import set_output_format as set_code_output_format

        set_code_output_format(output_format)
    except ImportError:
        pass
    app(tokens)


def main() -> None:
    from ..remote.oidc import RemoteAuthError
    from ..remote.proxy import (
        RemoteCredentialRejected,
        RemotePayloadTooLarge,
        RemoteToolFailed,
        RemoteToolUnavailable,
        RemoteUnreachable,
        RemoteWriteIndeterminate,
    )

    try:
        app.meta()
    # The seven ways a deployed witan fails a command, each already carrying its
    # own actionable wording: misconfigured or not logged in (RemoteAuthError),
    # reached but offering no such tool (RemoteToolUnavailable), not reached at
    # all (RemoteUnreachable), answering that the body is too big
    # (RemotePayloadTooLarge), cut off mid-write so that nobody can say whether
    # it landed (RemoteWriteIndeterminate), reached and refusing the credential
    # after a refresh already failed (RemoteCredentialRejected), and running the
    # tool only for the tool to refuse (RemoteToolFailed). All but the first
    # used to escape as a traceback.
    #
    # RemoteToolFailed is the wide one: only `migrate` has its own
    # `except RuntimeError`, so for every other command — memory, tasks,
    # projects, traces — this net is the entire difference between a Cedar
    # denial reading as a sentence and reading as a crash.
    except (
        RemoteAuthError,
        RemoteCredentialRejected,
        RemotePayloadTooLarge,
        RemoteToolFailed,
        RemoteToolUnavailable,
        RemoteUnreachable,
        RemoteWriteIndeterminate,
    ) as exc:
        # markup=False: these messages name config keys, and a target block is
        # written `[qa]` — which rich parses as a style tag and swallows, so
        # "unset `remote_url` on target [qa]" reached the user as "on target".
        # The one part of the sentence that identifies what to unset is exactly
        # the part markup ate.
        console.print(str(exc), style="red", markup=False)
        raise SystemExit(1) from None
