"""Hook helper commands: inject-context and session-checkpoint."""

from __future__ import annotations

import os
from pathlib import Path

from witan_core.observability import get_logger

from .. import config as cfg_module
from ._common import app
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
    debug: Print detection/read diagnostics (repo, branch, graph reads, counts,
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
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — never fail the hook
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
    """
    from .. import maintenance, session_state
    from ._common import _fn, _srv

    session_id = os.environ.get("CLAUDE_SESSION_ID") or ""
    handle = session_state.read_handle(session_id)
    session_slug = (handle or {}).get("session_slug")
    if session_slug:
        try:
            _fn(_srv().workflow_session_end)(
                session_slug=session_slug,
                summary=(
                    "Session ended (auto-closed by Stop hook — "
                    "call workflow_session_end explicitly for a better summary)"
                ),
                tools_used=None,
                files_changed=_changed_files() or None,
            )
        # SystemExit is not an Exception: _srv() raises it for a half-configured
        # remote, and letting it escape would break the "never blocks" contract.
        except (Exception, SystemExit):  # noqa: BLE001 — never fail the agent
            # Keep the handle. The close now goes over the network (token fetch
            # + MCP round-trip), so a failure here is usually transient — offline,
            # or an expired token needing `witan login`. Dropping the handle would
            # discard the only pointer to the session and leak it open forever;
            # keeping it lets the next Stop, or `witan session end`, finish the job.
            # Re-closing an already-ended session just re-stamps ended_at.
            pass
        else:
            session_state.clear_handle(session_id)

    # Keep the store compacted so query latency doesn't re-bloat. Runs at most
    # once per WITAN_OPTIMIZE_INTERVAL and detaches, so the Stop hook returns
    # immediately; best-effort, never fails the hook.
    #
    # The config load is inside the guard: `load()` raises ValueError on a
    # malformed config.toml or an unknown [targets.*] selection, and a broken
    # config must not turn into a failing Stop hook.
    try:
        maintenance.spawn_background_optimize(cfg_module.load().graph_uri)
    except Exception:  # noqa: BLE001 — maintenance must never fail the Stop hook
        pass


def _changed_files() -> list[str]:
    """Files dirty in the agent's checkout, for the auto-close record."""
    import subprocess

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", str(Path.cwd()))
    try:
        return subprocess.check_output(
            ["git", "-C", project_dir, "diff", "--name-only", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).splitlines()[:50]
    except (subprocess.CalledProcessError, OSError):
        return []
