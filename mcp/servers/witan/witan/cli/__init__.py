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
from .code_routing import warn_if_code_graph_is_local
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
    warn_if_code_graph_is_local()
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
    HTTP endpoint for a shared, deployed service — this is what the deployed
    witan tier runs, behind APISIX.

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
    # ★ HERE TOO, AND THIS IS THE PATH THAT MATTERS. `witan code …` mounts
    # witan_code's cyclopts App (`app.command(_code_app, name="code")`) but NOT
    # its meta launcher, so dispatch runs through THIS function and witan-code's
    # own launcher never executes. The output-format forwarding just below is
    # the tell: it exists precisely because witan-code's launcher — which sets
    # that itself — is bypassed.
    #
    # The CI indexer runs `witan code index .` (docker/witan-ci-index.sh), so
    # configuring observability only in witan-code's launcher would have left
    # the exact incident this change exists to surface just as silent.
    # Idempotent, and no-ops without the env vars; see witan_code.cli._launcher.
    from witan_core.observability import configure_observability

    configure_observability(instrument=False)
    try:
        from witan_code.output import set_output_format as set_code_output_format

        set_code_output_format(output_format)
    except ImportError:
        pass
    _warn_about_routing(tokens)
    app(tokens)


def _warn_about_routing(tokens: tuple[str, ...]) -> None:
    """Surface the local-code-graph warning on the path a person actually reads.

    ★ TWO GATES, AND BOTH ARE THE DIFFERENCE BETWEEN A SIGNAL AND NOISE.

    A tty, because everything else that runs this CLI is a machine: the Stop
    and context hooks, the CI indexer (``witan code index .``), a shell
    pipeline. None of them has a reader, and worse, any of them firing the
    warning would consume the throttle window below and buy the silence for the
    human who was supposed to get it — the same defect this call site exists to
    fix, one layer down.

    Not ``serve``, because ``_serve_target`` warns for itself, unthrottled, and
    a serve run must not spend the window either. Positional 0 is the whole
    test: cyclopts has already bound this function's own options by the time it
    is called, so the command name is the first token that survives.

    Not an explicit ``--target`` either, and that is the same argument a third
    time. This runs BEFORE ``app(tokens)``, which is the call that binds a
    command's arguments — so all this can resolve is the ambient target
    (``WITAN_TARGET``, else the checkout's ``match_*``), which under
    ``witan whoami --target qa`` is a DIFFERENT target from the one the command
    reports on. Warning then answers about a target nobody asked about and,
    worse, stamps that target's throttle file: the run that should have warned
    the human is silenced for a day by a run that was about something else.
    Under-warning here is the safe direction, and the gap is small — every
    command that takes ``--target`` either prints the routing itself
    (``whoami``'s ``Code`` line) or is not an indexing command.

    Presence is all this checks, never which command the flag binds to: argv
    cannot tell you that (``witan run <agent> --target qa`` may be the agent's
    flag), and guessing is how the wrong target gets warned about again.
    Making ``--target`` a real app-level option is the actual fix — see
    tk-target-is-a-per-command-flag-so-the-pre-dispatch-44ea22, which also
    records why that cannot be done additively.
    """
    if not stderr_console.is_terminal:
        return
    if tokens[:1] == ("serve",):
        return
    if any(t == "--target" or t.startswith("--target=") for t in tokens):
        return
    warn_if_code_graph_is_local(throttle=True)


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
