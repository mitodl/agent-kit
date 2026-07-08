"""Hook helper commands: inject-context and session-checkpoint."""

from __future__ import annotations

from pathlib import Path

from .. import config as cfg_module
from ._common import app


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
    import sys

    from .. import context as ctx_module

    cfg = cfg_module.load()
    graph_path = (
        Path(cfg.graph_uri)
        if not cfg.graph_uri.startswith(("http://", "https://", "s3://"))
        else None
    )
    if graph_path is not None and not graph_path.exists():
        if debug:
            print(
                f"[witan inject-context] graph file does not exist: {graph_path} "
                "(run `witan setup` / `install.sh`?)",
                file=sys.stderr,
            )
        return
    text = ctx_module.inject_context(
        cfg.graph_uri, cfg.queries_dir, cfg.graph_token, debug=debug
    )
    if text:
        print(text)


@app.command(name="session-checkpoint")
def session_checkpoint() -> None:
    """Auto-close the active WorkflowSession on agent stop (Stop hook).

    Reads the state file written by ``workflow_session_start`` and records an
    end timestamp via ``update_workflow_session_end``. No-op when the file is
    absent — always exits 0 and never blocks.
    """
    from .. import context as ctx_module

    cfg = cfg_module.load()
    ctx_module.session_checkpoint(cfg.graph_uri, cfg.queries_dir, cfg.graph_token)
