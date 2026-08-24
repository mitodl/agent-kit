"""Shared fixtures for witan tests.

Integration tests spin up a throwaway omnigraph graph per test and point the
FastMCP server's client at it, so the real query/mutation files and the real
omnigraph binary are exercised end-to-end. Tests are skipped when the binary
is not on PATH.
"""

import asyncio
import inspect
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema" / "schema.pg"

omnigraph_available = shutil.which("omnigraph") is not None
requires_omnigraph = pytest.mark.skipif(
    not omnigraph_available, reason="omnigraph binary not on PATH"
)


def _unwrap(tool):
    """Return the underlying function for a FastMCP-decorated tool."""
    return getattr(tool, "fn", tool)


class _NoElicitCtx:
    """A stand-in Context whose ``elicit`` always errors — simulates a client
    without elicitation support, so async tools fall back to their default
    (non-interactive) behavior. Tests that exercise elicitation pass their own
    fake ctx via ``ctx=...`` instead."""

    async def elicit(self, *args, **kwargs):
        raise RuntimeError("elicitation unsupported in tests")


class _Tools:
    """Attribute proxy that returns unwrapped, directly-callable tools.

    Async tools (those taking a ``ctx: Context``) are run to completion via
    ``asyncio.run`` with a no-elicit ctx injected, so the 50+ existing sync call
    sites keep working unchanged and get today's non-interactive behavior."""

    def __init__(self, module):
        self._module = module

    def __getattr__(self, name):
        fn = _unwrap(getattr(self._module, name))
        if inspect.iscoroutinefunction(fn):

            def runner(*args, **kwargs):
                kwargs.setdefault("ctx", _NoElicitCtx())
                return asyncio.run(fn(*args, **kwargs))

            return runner
        return fn


@pytest.fixture(autouse=True)
def no_real_remote(tmp_path_factory, monkeypatch):
    """Keep the developer's own ``[targets.*]`` out of the test run.

    Config resolution reads the REAL ``~/.config/witan/config.toml``, so on a
    machine whose current checkout matches a deployed target it answers with
    that deployment — and any test that reaches routing then behaves
    differently depending on who ran it, or talks to production outright.

    Not hypothetical: ``witan serve`` dispatches to the deployment when a
    remote target matches, so with a developer's production target in scope
    ``test_serve_defaults_to_stdio`` built a real proxying server and
    ``serve()`` blocked forever on a genuine stdio listener. Autouse, for the
    same reason ``no_background_optimize`` exists: the unit suite must not
    depend on — or touch — a real deployment.

    ★ Isolates the config SOURCE rather than stubbing ``load_remote_config``.
    Stubbing it is the obvious move and it is wrong: a dozen tests in
    test_config.py exist to check what that function returns, and a stub makes
    every one of them assert against the stub. Pointing ``WITAN_CONFIG`` at a
    path that does not exist leaves the real resolution running — it simply
    finds no targets. Tests that need targets set ``WITAN_CONFIG`` themselves,
    and the later ``setenv`` wins.

    The two environment overrides are cleared for the same reason: they bypass
    the file entirely, so leaving them set would reintroduce exactly the leak
    the temp path closes.
    """
    absent = tmp_path_factory.mktemp("witan-config") / "config.toml"
    monkeypatch.setenv("WITAN_CONFIG", str(absent))
    monkeypatch.delenv("WITAN_REMOTE_URL", raising=False)
    monkeypatch.delenv("WITAN_TARGET", raising=False)


@pytest.fixture(autouse=True)
def no_real_merge_watermarks(tmp_path_factory, monkeypatch):
    """Keep merge watermarks out of the developer's real ``~/.config/witan``.

    ``witan migrate merge`` records a per-pair watermark, and the default path
    is a REAL user file next to the token cache. Any test that drives the merge
    CLI without overriding ``WITAN_MERGE_WATERMARKS`` therefore writes to the
    developer's own state.

    Not hypothetical, and not caught by the suite: on 2026-08-24 the file had
    ten entries keyed by ``/tmp/pytest-of-*/…/personal.omni`` from a single
    afternoon's runs — accumulating one per run, since each pytest tmp path is
    unique so nothing ever replaced anything. It surfaced only because the file
    was opened by hand before a real merge.

    Autouse for the same reason as ``no_real_remote`` above: the leak is a
    property of forgetting an override, so the guard has to be the default
    rather than something each test opts into. Tests that assert on watermark
    contents set the variable themselves, and the later ``setenv`` wins.
    """
    marks = tmp_path_factory.mktemp("witan-watermarks") / "merge-watermarks.json"
    monkeypatch.setenv("WITAN_MERGE_WATERMARKS", str(marks))


@pytest.fixture
def tmp_state_dir(tmp_path, monkeypatch):
    """Redirect the system temp dir, which is where witan parks process state.

    ``workflow_session_start`` parks a session handle there and ``maintenance``
    keeps its optimize throttle stamp there, so without this a session-starting
    test litters the developer's real ``$TMPDIR`` with handle files. Note the
    redirect has to happen here rather than by patching a name on
    ``witan.server``: the readers resolve the path through
    ``session_state.session_state_path`` directly.
    """
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)
    return tmp_path


@pytest.fixture
def no_background_optimize(monkeypatch):
    """Stop ``session_checkpoint`` from touching the developer's real store.

    The Stop hook ends with ``spawn_background_optimize(cfg.graph_uri)``, and
    ``cfg_module.load()`` reads the *real* config — the ``server`` fixture
    isolates ``srv.client``, not the graph URI. Redirecting ``TMPDIR`` also hides
    the throttle stamp, so an optimize looks due on every run and would fork a
    detached ``witan optimize`` against ``~/.local/share/witan/graph.omni``.
    ``0`` disables auto-optimize; ``test_maintenance.py`` sets its own value.
    """
    monkeypatch.setenv("WITAN_OPTIMIZE_INTERVAL", "0")


@pytest.fixture
def server(tmp_path, monkeypatch, tmp_state_dir, no_background_optimize):
    if not omnigraph_available:
        pytest.skip("omnigraph binary not on PATH")

    store = tmp_path / "graph.omni"
    subprocess.run(
        ["omnigraph", "init", "--schema", str(SCHEMA), str(store)],
        check=True,
        capture_output=True,
        text=True,
    )

    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/repo")
    monkeypatch.setenv("WITAN_AUTHOR", "pytest")
    # Isolate from the real agent session: memory_store auto-wires a
    # SessionProduced edge when CLAUDE_SESSION_ID resolves to a live session.
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import server as srv

    client = graph_mod.OmnigraphClient(str(store), cfg_mod.load().queries_dir)
    monkeypatch.setattr(srv, "client", client)
    return _Tools(srv)
