"""Hook helper commands: inject-context and session-checkpoint."""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

from witan_core.observability import get_logger

from .. import config as cfg_module
from .. import session_summary
from ._common import _fn, app
from .selected_target import selected_target

logger = get_logger("witan.hook")


@app.command(name="inject-context")
def inject_context(*, debug: bool = False) -> None:
    """Print workflow context for the UserPromptSubmit hook.

    Emits active WorkflowProjects and ready Tasks for the current git repo to
    stdout. Designed to be called by ``~/.claude/hooks/workflow-context-inject.sh``
    — always exits 0 and never blocks even when the graph is missing or the repo
    is not in git.

    Parameters
    ----------
    debug:
        Print detection/read diagnostics (repo, branch, graph reads, counts,
        and the reason for any swallowed failure) to stderr. stdout still carries
        only the injected block, so ``witan inject-context --debug`` is safe to
        run by hand to see why the block is blank.
    """
    from .. import context as ctx_module

    # The config load is the earliest step and was the one unguarded one, so it
    # failed before any of the machinery below could help. `load()` raises
    # ValueError on a malformed config.toml — `load_toml` fails the whole
    # document by design, so one stray character in a `[targets.*]` table takes
    # out context injection entirely — and also for a `WITAN_TARGET` naming a
    # target that isn't defined, which breaks the hook with a perfectly valid
    # config file. SystemExit is not an Exception and `load_remote_config()`
    # raises it for a half-configured remote; letting either escape breaks the
    # "never blocks" contract this command documents. Same guard as
    # `session-checkpoint`.
    try:
        remote = cfg_module.load_remote_config(target=selected_target())
    except (Exception, SystemExit) as exc:  # never fail the hook
        if debug:
            logger.debug("witan.hook.config_load_failed", error=str(exc), exc_info=True)
        return

    if remote is not None:
        # A `remote_url`-only target has no direct graph endpoint to hand
        # OmnigraphClient — that fallback is exactly #261's mechanism
        # (tk-witan-hook-context-reads-the-local-store-on-a-de-dfb2c9), so a
        # deployed target reads through the same tool-calling proxy `_srv()`
        # would build, not `cfg_module.load()`'s local-store `graph_uri`.
        from ._common import remote_proxy

        text = ctx_module.inject_context_remote(
            remote_proxy(remote), remote.url, debug=debug
        )
        queued = ctx_module.journal_line(remote.url, debug=debug)
        text = "\n\n".join(part for part in (queued, text) if part)
        if text:
            print(text)
        return

    try:
        cfg = cfg_module.load()
    except (Exception, SystemExit) as exc:  # never fail the hook
        if debug:
            logger.debug("witan.hook.config_load_failed", error=str(exc), exc_info=True)
        return
    graph_path = (
        Path(cfg.graph_uri)
        if not cfg.graph_uri.startswith(("http://", "https://", "s3://"))
        else None
    )
    if graph_path is not None and not graph_path.exists():
        if debug:
            logger.debug(
                "witan.hook.graph_missing",
                path=str(graph_path),
                hint="run `witan setup` / `install.sh`?",
            )
        return
    text = ctx_module.inject_context(
        cfg.graph_uri,
        cfg.queries_dir,
        cfg.graph_token,
        debug=debug,
        graph_id=cfg.graph_name,
        author=cfg.author,
        s3_profile=cfg.s3_profile,
        s3_region=cfg.s3_region,
    )
    if text:
        print(text)


@app.command(name="session-checkpoint")
def session_checkpoint() -> None:
    """Auto-close the active WorkflowSession on agent stop (Stop hook).

    Reads the session handle ``workflow_session_start`` returned (persisted
    locally, see ``witan.session_state``) and passes its ``session_slug`` back to
    ``workflow_session_end``. No-op when there is no handle — the session was
    already closed explicitly. Always exits 0 and never blocks. Also
    opportunistically triggers a throttled background store compaction.

    The end call goes through ``_srv()``, so it reaches whichever server actually
    holds the session: the in-process module locally, or the deployment over MCP.
    Writing straight to a local store here is what used to leave deployed
    sessions open forever.

    Under Codex, whose hooks get no session variable, the session id comes
    from the hook's stdin payload instead.

    The summary is derived from what the session left behind (see
    ``witan.session_summary``), because the next session's handoff shows it
    and agents rarely write one. A session already ended, by an explicit
    ``workflow_session_end`` or an earlier Stop, is not closed again: its
    summary stands and only the stale handle is dropped.
    """
    from .. import maintenance, session_state
    from ._common import _srv

    # $CLAUDE_SESSION_ID under Claude Code's Stop hook; $PI_SESSION_ID under
    # Pi, whose workflow extension sets it on this process at session_shutdown;
    # the payload's `session_id` under Codex.
    session_id = session_state.current_session_id(_codex_session_id(_hook_payload()))
    handle = session_state.read_handle(session_id)
    session_slug = (handle or {}).get("session_slug")
    if session_slug:
        try:
            # Codex fails a Stop hook whose stdout isn't a JSON decision, and
            # `_srv()` prints its half-configured-remote error to stdout.
            with contextlib.redirect_stdout(sys.stderr):
                server = _srv()
                session = _read_session(server, handle)
                # An ended session was closed by an explicit
                # `workflow_session_end` that left this handle behind (one
                # called through the remote proxy clears nothing locally), or
                # by an earlier Stop whose response was lost. Either way its
                # summary wins over anything derived here, so only the handle
                # goes.
                if session is None or not session.get("ended_at"):
                    project_dir = _project_dir()
                    _fn(server.workflow_session_end)(
                        session_slug=session_slug,
                        summary=_derived_summary(
                            server, handle, session, session_id, project_dir
                        ),
                        tools_used=None,
                        files_changed=session_summary.changed_files(project_dir)
                        or None,
                    )
        # SystemExit is not an Exception: _srv() raises it for a half-configured
        # remote, and letting it escape would break the "never blocks" contract.
        except (Exception, SystemExit):  # noqa: BLE001, S110 — never fail the agent
            # Keep the handle. The close now goes over the network (token fetch
            # + MCP round-trip), so a failure here is usually transient — offline,
            # or an expired token needing `witan login`. Dropping the handle would
            # discard the only pointer to the session and leak it open forever;
            # keeping it lets the next Stop, or `witan session end`, finish the job.
            # A close that landed but whose response was lost is found ended by
            # that next Stop and left as it is.
            pass
        else:
            session_state.clear_handle(session_id)

    # Keep the store compacted so query latency doesn't re-bloat. Runs at most
    # once per WITAN_OPTIMIZE_INTERVAL (cleaning up once per
    # WITAN_CLEANUP_INTERVAL) and detaches, so the Stop hook returns
    # immediately; best-effort, never fails the hook.
    #
    # The config load is inside the guard: `load()` raises ValueError on a
    # malformed config.toml or an unknown [targets.*] selection, and a broken
    # config must not turn into a failing Stop hook.
    try:
        maintenance.spawn_background_optimize(cfg_module.load().graph_uri)
    except Exception:  # noqa: BLE001, S110 — maintenance must never fail the Stop hook
        pass


def _hook_payload() -> dict:
    """The hook's stdin JSON object, or ``{}`` when there isn't one.

    Empty for a terminal (``witan session-checkpoint`` run by hand), for Pi's
    extension, which spawns the command with stdin ignored, and for anything
    that isn't a JSON object. Claude Code and Codex write the payload and
    close the pipe, so the read can't block.
    """
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _codex_session_id(payload: dict) -> str | None:
    """The session id from a Codex hook payload, or ``None`` for any other.

    Codex runs a hook with its own environment and no session variable, so
    stdin is the only place the id is. It is the same id Codex exports to its
    shell tool as ``$CODEX_SESSION_ID``, which is what the agent passes to
    ``workflow_session_start`` and so what the parked handle is keyed by.

    Told apart by ``turn_id``, which Codex's input schema marks as a Codex
    extension to the Claude-style payload (``codex-rs/hooks/src/schema.rs``).
    A payload without it is not used, so the Claude path still resolves
    ``$CLAUDE_SESSION_ID`` exactly as before.
    """
    if "turn_id" not in payload:
        return None
    return payload["session_id"]


def _project_dir() -> str:
    """The agent's checkout: ``$CLAUDE_PROJECT_DIR`` under Claude Code, else cwd.

    Pi's extension spawns the checkpoint in the session's cwd, and Codex runs
    its hooks there, so the working directory is right for both.
    """
    return os.environ.get("CLAUDE_PROJECT_DIR") or str(Path.cwd())


def _read_session(server, handle: dict) -> dict | None:
    """The handle's session row, or ``None`` when it can't be read.

    ``None`` covers a handle with no ``project_slug`` (one parked by hand or by
    an older client) and a failed read. The caller then closes the session as
    it always did, rather than refusing to close what it can't see.
    """
    project_slug = handle.get("project_slug")
    if not project_slug:
        return None
    try:
        rows = _fn(server.workflow_session_list)(project_slug=project_slug)
    except Exception:  # a failed read must not stop the close
        logger.debug("witan.hook.session_read_failed", exc_info=True)
        return None
    return next((r for r in rows if r.get("slug") == handle["session_slug"]), None)


def _derived_summary(
    server, handle: dict, session: dict | None, session_id: str, project_dir: str
) -> str:
    """The auto-close summary, built from what this session left in the graph and git.

    Each source is read separately and a failed one is named in the summary
    rather than read as "nothing happened". Without a start time there is no
    window to attribute anything to, so the placeholder is all that's true.
    Never raises: anything unexpected also falls back to the placeholder,
    because the close itself matters more than what it says.
    """
    try:
        started_at = (session or {}).get("started_at") or handle.get("started_at")
        since = session_summary.parse_timestamp(started_at)
        project_slug = handle.get("project_slug")
        if since is None or not project_slug:
            return session_summary.PLACEHOLDER
        activity = session_summary.Activity()
        try:
            rows = _fn(server.task_list)(project_slug=project_slug)
        except Exception:
            logger.debug("witan.hook.task_read_failed", exc_info=True)
            activity.unread.append("tasks")
        else:
            activity.closed_tasks, activity.claimed_tasks = (
                session_summary.session_tasks(
                    rows, handle.get("session_id") or session_id, since
                )
            )
        try:
            produced = _fn(server.workflow_project_memories)(
                project_slug=project_slug, session=handle["session_slug"]
            )
        except Exception:
            logger.debug("witan.hook.memory_read_failed", exc_info=True)
            activity.unread.append("memories")
        else:
            activity.memories = produced["memories"]
        session_summary.git_activity(activity, project_dir, since)
        return session_summary.render(activity)
    except Exception:
        logger.debug("witan.hook.summary_failed", exc_info=True)
        return session_summary.PLACEHOLDER
