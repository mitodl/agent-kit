"""Where this config sends code graphs, and saying so where people read it.

Memory and code graphs are routed by SEPARATE settings — ``remote_url`` sends
the work/memory graph to a deployment, ``code_transport = "mcp"`` (or a
``code_server``) sends the code graphs — and nothing ties them together. A
target that sets the first and not the second gives you an agent whose memory
is shared and whose code graph is a directory on one laptop, which defeats the
point of indexing a branch at all: branches are indexed per writer so another
session, and another developer, can see work still in flight.

★ THE DETECTION WAS ALREADY CORRECT; ITS REACH WAS THE DEFECT. This lived in
``witan serve`` alone, whose stderr belongs to the agent harness and is in
practice swallowed. Three non-maintainers were in exactly this state on
production with nothing telling them, because everything they ran — ``witan
tasks``, ``witan memory``, ``witan code index`` — was silent about it.

A warning rather than a refusal, for the reason it always was: a local code
graph is a legitimate choice for someone who has not provisioned cluster
graphs. The problem is reach, not severity.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from rich.markup import escape
from witan_core import maintenance as _throttle

from .. import config as cfg_module
from .. import session_state
from ._common import stderr_console

# How often the same target may re-warn. A warning on all 40 commands gets
# filtered out by the reader, which is the same failure by a different route.
WARN_INTERVAL_ENV_VAR = "WITAN_LOCAL_CODE_GRAPH_WARN_INTERVAL"
DEFAULT_WARN_INTERVAL = 24 * 3600.0


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


def _code_config(target: str | None = None):
    """``witan_code``'s resolved config, or ``None`` if there is nothing to say.

    Both misses are deliberate. witan-code may not be installed — the umbrella
    works standalone. And a config this cannot parse is not this function's
    error to report: the command the user actually ran raises on the same load
    with a message about the key that is wrong, and pre-empting it with a
    routing warning would bury that.
    """
    try:
        from witan_code import config as code_cfg_module
    except ImportError:
        return None
    try:
        return code_cfg_module.load(target=target)
    except ValueError:
        return None


def code_graph_destination(target: str | None = None) -> str | None:
    """One line answering "where do my indexed branches go?", or ``None``.

    ``witan whoami`` is the command people run to ask what they are pointed at,
    and until this it answered only the identity half.
    """
    cfg = _code_config(target)
    if cfg is None:
        return None
    if not cfg.is_cluster:
        return f"local to this machine, under {cfg.code_dir}"
    if cfg.code_server:
        return f'shared, at {cfg.code_server} (code_transport = "direct")'
    return 'shared, through this endpoint (code_transport = "mcp")'


def _stamp_file(target_name: str | None) -> Path:
    # Keyed on the target so switching to one that is misrouted warns straight
    # away, rather than inheriting the silence of the one just left.
    digest = hashlib.sha1((target_name or "").encode()).hexdigest()[:16]  # noqa: S324
    return session_state.session_state_dir() / f"witan-code-local-{digest}.json"


def warn_if_code_graph_is_local(*, throttle: bool = False) -> None:
    """Warn when the memory graph is deployed but code graphs are not.

    ``throttle`` is for the CLI dispatch path, which reaches every command: it
    both suppresses a repeat inside the window and records that the warning was
    shown. ``witan serve`` passes it off — its stderr may have no reader, so a
    serve run must not consume the one warning a human would otherwise get.
    """
    try:
        remote = cfg_module.load_remote_config()
    except ValueError:
        # Same reasoning as `_code_config`: the command being run reports this.
        return
    if remote is None:
        return
    cfg = _code_config()
    if cfg is None or cfg.is_cluster:
        return
    stamp = _stamp_file(cfg.target_name)
    now = time.time()
    if throttle:
        interval = _throttle.resolve_interval(
            WARN_INTERVAL_ENV_VAR, DEFAULT_WARN_INTERVAL
        )
        if interval <= 0 or now - _throttle.last_run(stamp) < interval:
            return
    stderr_console.print(_local_code_graph_warning(cfg.code_transport, cfg.target_name))
    if throttle:
        _throttle.mark_run(stamp, now)
