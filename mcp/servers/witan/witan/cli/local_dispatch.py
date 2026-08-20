"""Stop the CLI writing to the local store while the caller believes otherwise.

The twin of the ``witan serve`` defect fixed in agent-kit#261, on the other
path out of the same config file. Target selection is contextual — a
``[targets.production]`` block claims ``~/code/mit`` via ``match_paths`` — so
the same command is routed to the deployment from one directory and to
``~/.local/share/witan/graph.omni`` from another. The store changed; nothing in
the output did. ``witan task close`` printed ``Closed <slug>`` and the full
resolution text, exited 0, and the deployed graph still showed the task open
nine days later.

That is worse than a plain misconfiguration, because every signal says it
worked. It is also aimed squarely at a new user: someone who has just run
``witan login`` will not have the maintainer's ``~/code/mit`` layout, will run
witan from wherever they happen to be, and would get cheerful success messages
while their onboarding produced nothing in the shared graph.

So writes refuse and say why, and a local store is never used silently.
Reads are allowed to fall back — a stale read is recoverable in a way a write
to the wrong graph is not — but still announce where they read from.
"""

from __future__ import annotations

from ..config import LocalDispatch
from ._common import stderr_console

__all__ = ["READ_TOOLS", "guard_local_store", "local_write_refused"]

READ_TOOLS = frozenset(
    {
        "memory_search",
        "memory_list",
        "memory_get",
        "memory_neighbors",
        "memory_for_contract",
        "memory_symbols",
        "topic_get",
        "recall",
        "symbol_context",
        "task_get",
        "task_list",
        "task_ready",
        "workflow_project_get",
        "workflow_project_status",
        "workflow_project_list",
        "workflow_project_memories",
        "workflow_project_get_blockers",
        "workflow_trace_list",
        "workflow_trace_get",
        "workflow_session_list",
    }
)
"""Tools that only read, and so may fall back to a local store.

An allowlist rather than a list of writes, because the two directions fail
differently. A write missing from a write-list is dispatched silently — the
exact defect this module exists to stop — whereas a read missing from this one
is merely refused, and says so. New tools therefore default to refusing, and
``test_local_dispatch.py`` asserts every name here is really a server tool so
the set cannot drift into naming something that no longer exists.
"""


def local_write_refused(tool: str, diagnosis: LocalDispatch) -> str:
    """Explain the refusal, naming the store, the miss, and all three ways out.

    Written for someone who has no reason to suspect routing exists: they ran a
    command, and it stopped. So it states which graph it was about to write, why
    that is not the shared one, and how to reach either — rather than the
    ``remote mode is not configured`` line the CLI used to keep to itself and
    print only if you separately ran ``witan whoami``.
    """
    deployed = ", ".join(diagnosis.deployed_targets)
    return (
        f"witan: refusing to run `{tool}` against a local store.\n"
        f"\n"
        f"  would write   {diagnosis.graph_uri}\n"
        f"  deployed      {deployed}\n"
        f"\n"
        "No target matches this directory, so nothing selected the deployed "
        "graph and the write would have landed on this machine — and reported "
        "success, with no mention of which graph took it.\n"
        "\n"
        "Choose one:\n"
        "  run the command from a checkout one of those targets matches, or\n"
        f"  name the deployment:  witan --target {diagnosis.deployed_targets[0]} …"
        f"  (or WITAN_TARGET)\n"
        "  write locally on purpose:  WITAN_MEMORY_URI=<path> witan …"
    )


def local_read_notice(diagnosis: LocalDispatch) -> str:
    """The one-line banner naming the local store a command is about to use.

    Printed whenever a deployment is configured and the CLI is not using it,
    which is the whole ambiguity: with both a local and a deployed target in
    one config file, identical output means two different graphs. Cheap enough
    to always print, and stderr keeps it out of ``--output-format json``.
    """
    where = f"target [{diagnosis.target_name}]" if diagnosis.target_name else "no match"
    return (
        f"[dim]witan: reading local store {diagnosis.graph_uri} "
        f"({where}) — deployed targets: "
        f"{', '.join(diagnosis.deployed_targets)}[/dim]"
    )


class _LocalStoreGuard:
    """Passes reads through to the in-process server; refuses everything else.

    A proxy rather than a check at each call site. There are ~40 dispatch
    points across the CLI package and they are not uniform — most go through
    ``_fn(s.tool)``, ``witan migrate`` calls ``s.tool()`` directly — so a
    per-site guard would be a list to keep in sync, and the one site somebody
    forgets is indistinguishable from the bug. Attribute access is the single
    place every one of them passes through.
    """

    def __init__(self, inner, diagnosis: LocalDispatch) -> None:
        self._inner = inner
        self._diagnosis = diagnosis
        self._announced = False

    def __getattr__(self, name: str):
        # Dunders reach the inner object untouched: they are protocol, not
        # tools, and refusing e.g. `__class__` would break ordinary
        # introspection long before any write was attempted.
        if name.startswith("__"):
            return getattr(self._inner, name)
        if name in READ_TOOLS:
            if not self._announced:
                self._announced = True
                stderr_console.print(local_read_notice(self._diagnosis))
            return getattr(self._inner, name)
        stderr_console.print(f"[red]{local_write_refused(name, self._diagnosis)}[/red]")
        raise SystemExit(1)


def guard_local_store(inner, diagnosis: LocalDispatch | None):
    """Wrap the in-process server when its store was not chosen deliberately.

    ``diagnosis is None`` means no deployment is configured at all, so there is
    no other graph this could have meant — the local store is simply witan, and
    the CLI behaves exactly as it did before this module existed.
    """
    if diagnosis is None:
        return inner
    if diagnosis.deliberate:
        stderr_console.print(local_read_notice(diagnosis))
        return inner
    return _LocalStoreGuard(inner, diagnosis)
