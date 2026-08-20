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
to the wrong graph is not — but still announce which store answered.

★ REFUSING IS NOT ENOUGH ON ITS OWN; THE STORE MUST ALSO NOT BE TOUCHED.
Importing ``witan.server`` runs ``_ensure_graph(cfg.graph_uri)`` at module
scope, which CREATES a missing local store and re-applies its schema. A guard
that imported the server and then refused would have already done both by the
time it said no — and on a fresh machine could fail while initialising a store
the caller never wanted. So the diagnosis happens first and the import is
deferred until an allowed read actually asks for it.
"""

from __future__ import annotations

from ..config import LocalDispatch
from ._common import stderr_console

__all__ = [
    "CLIENT_READ_ATTRS",
    "READ_TOOLS",
    "local_server",
    "local_store_notice",
    "local_write_refused",
]

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

CLIENT_READ_ATTRS = frozenset({"read", "graph_uri"})
"""What ``s.client.<attr>`` may reach on a fallback store.

Several read-only commands go around the tool surface and query the client
directly — ``witan session list`` (``session.py``), ``witan trace show``
(``traces.py``) and ``witan project show`` (``projects.py``) all call
``s.client.read(...)``. Refusing the whole ``client`` attribute would break
three working read commands with a message about writes.

Handing back the real client instead would be worse: it also carries
``change``/``change_many``/``load``, so the guard would be trivially
side-steppable by the one code path that already bypasses the tool layer.
Hence a facade over exactly the two members those commands use — the query
call, and the store path ``witan migrate storage`` prints. Anything else on
the client refuses like any other write.
"""


def local_write_refused(tool: str, diagnosis: LocalDispatch) -> str:
    """Explain the refusal, naming the store, the miss, and all three ways out.

    Written for someone who has no reason to suspect routing exists: they ran a
    command, and it stopped. So it states which graph it was about to write, why
    that is not the shared one, and how to reach either — rather than the
    ``remote mode is not configured`` line the CLI used to keep to itself and
    print only if you separately ran ``witan whoami``.

    ``WITAN_TARGET`` rather than ``--target``: the flag exists only on the
    handful of commands that take one (``login``, ``logout``, ``whoami``,
    ``run``, ``migrate merge``), and notably not on ``task close`` — the
    command that produced this report. The environment variable is read by
    ``config._select_target`` for every command, so it is the form that always
    works. Naming a flag the reader's command does not accept would leave them
    exactly where they started.
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
        f"  name the deployment:  WITAN_TARGET={diagnosis.deployed_targets[0]} witan …\n"
        "  write locally on purpose:  WITAN_MEMORY_URI=<path> witan …"
    )


def local_store_notice(diagnosis: LocalDispatch) -> str:
    """The one-line banner naming the local store a command is about to use.

    Printed whenever a deployment is configured and the CLI is not using it,
    which is the whole ambiguity: with both a local and a deployed target in
    one config file, identical output means two different graphs. Cheap enough
    to always print, and stderr keeps it out of ``--output-format json``.

    "using", not "reading" — on the deliberate path this fires for writes too,
    and a write announcing itself as a read is its own small lie about where
    the data went.
    """
    where = f"target [{diagnosis.target_name}]" if diagnosis.target_name else "no match"
    return (
        f"[dim]witan: using local store {diagnosis.graph_uri} "
        f"({where}) — deployed targets: "
        f"{', '.join(diagnosis.deployed_targets)}[/dim]"
    )


def _import_server():
    """Import ``witan.server``, which is what opens the local store.

    Its own function so the guard can hold it unevaluated — see this module's
    docstring on why importing before deciding would defeat the point.
    """
    from .. import server as server_module

    return server_module


class _ReadOnlyClient:
    """``s.client`` narrowed to the query surface the read commands use.

    See :data:`CLIENT_READ_ATTRS`. Delegates the load/announce/refuse decisions
    back to the guard so a client read counts as the guard's first use and
    prints the same banner a tool read would.
    """

    def __init__(self, guard: _LocalStoreGuard) -> None:
        self._guard = guard

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        if name in CLIENT_READ_ATTRS:
            return getattr(self._guard._allow().client, name)
        self._guard._refuse(f"client.{name}")
        raise AssertionError("unreachable")  # pragma: no cover


class _LocalStoreGuard:
    """Passes reads through to the in-process server; refuses everything else.

    A proxy rather than a check at each call site. There are ~50 dispatch
    points across the CLI package and they are not uniform — most go through
    ``_fn(s.tool)``, ``witan migrate`` calls ``s.tool()`` directly, and three
    read commands reach past both into ``s.client.read(...)`` — so a per-site
    guard would be a list to keep in sync, and the one site somebody forgets is
    indistinguishable from the bug. Attribute access is the single place every
    one of them passes through.

    Holds the server module UNIMPORTED until an allowed read asks for it,
    because importing it is itself a write to the store this refuses to use.
    """

    def __init__(self, load_inner, diagnosis: LocalDispatch) -> None:
        self._load_inner = load_inner
        self._diagnosis = diagnosis
        self._inner = None
        self._announced = False

    def _allow(self):
        """Return the server module, importing and announcing it on first use."""
        if not self._announced:
            self._announced = True
            stderr_console.print(local_store_notice(self._diagnosis))
        if self._inner is None:
            self._inner = self._load_inner()
        return self._inner

    def _refuse(self, what: str) -> None:
        stderr_console.print(f"[red]{local_write_refused(what, self._diagnosis)}[/red]")
        raise SystemExit(1)

    def __getattr__(self, name: str):
        # Dunders are protocol, not tools. Raise rather than delegate: reaching
        # the inner object would import the server, which is the side effect
        # this class exists to defer.
        if name.startswith("__"):
            raise AttributeError(name)
        if name == "client":
            return _ReadOnlyClient(self)
        if name in READ_TOOLS:
            return getattr(self._allow(), name)
        self._refuse(name)
        raise AssertionError("unreachable")  # pragma: no cover


def local_server(diagnosis: LocalDispatch | None, load_inner=_import_server):
    """The in-process server, guarded when its store was not chosen deliberately.

    ``diagnosis is None`` means no deployment is configured at all, so there is
    no other graph this could have meant — the local store is simply witan, and
    the CLI behaves exactly as it did before this module existed.

    ``load_inner`` is injectable so tests can assert the import really is
    deferred; nothing in production passes it.
    """
    if diagnosis is None:
        return load_inner()
    if diagnosis.deliberate:
        stderr_console.print(local_store_notice(diagnosis))
        return load_inner()
    return _LocalStoreGuard(load_inner, diagnosis)
