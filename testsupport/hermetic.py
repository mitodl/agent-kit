"""Point every suite's ambient state at a throwaway directory, before import.

── THE DEFECT CLASS ──

A test that reads or writes the machine it runs on instead of state it set up
itself. It passes in CI and only in CI, because a GitHub runner is the empty
case of every ambient input there is: no ``witan login``, no agent configs, no
``~/.gitconfig``, and a terminal wide enough that nothing wraps. A test that
asserts any of those *absences* is asserting the runner, not a behaviour.

It has cost real time in three packages already, each found by a person rather
than by a check:

  agent-kit#282   witan's merge tests wrote to the developer's real
                  ``~/.config/witan/merge-watermarks.json``, one unbounded
                  entry per run. Found by opening the file by hand.
  agent-kit#285   four witan-code tests asserted the logged-out branch of the
                  branch-view write guard, so they failed for anyone who had
                  run ``witan login``. Three unrelated PRs each paid a
                  stash-and-rerun to prove they were pre-existing.
  (this change)   19 of witan's 49 test files created a real graph in
                  ``~/.local/share/witan``, and ``witan-code``'s
                  ``test_ingest.py`` created a real code store in
                  ``~/.local/share/witan/code``. Silently: every test passed.

── WHY THIS RUNS AT IMPORT, NOT IN A FIXTURE ──

★ A per-test fixture is structurally too late for the largest case. Importing
``witan.server`` IS a write — ``_ensure_graph`` creates the local store at
module scope, deliberately, because the CLI's local-dispatch guard depends on
it (see that function's docstring). That import happens during COLLECTION, so
by the time any autouse fixture body runs, the store already exists. Hence the
redirection below happens when this module is imported, which a rootdir
``conftest.py`` does before pytest imports a single test module.

That makes the fake home session-scoped rather than per-test. Deliberate: the
goal is "never touch the real machine", not "isolate tests from each other",
and per-test isolation is what ``tmp_path`` is already for.

── WHAT IS *NOT* REDIRECTED ──

``PATH`` keeps its entry for the real ``~/.local/bin``, because that is where
CI installs the omnigraph binary and where ``OmnigraphClient._find_binary``
looks when ``PATH`` misses. A binary is a tool, not state: relocating it would
break every test that needs a graph, in service of a leak that cannot happen —
nothing writes there.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

__all__ = ["FAKE_HOME", "REAL_HOME", "STRICT_ENV_VAR"]

# Captured BEFORE the redirection below, so the PATH entry we re-add points at
# the real one. Order in this module is load-bearing; do not reorder.
REAL_HOME = Path.home()

FAKE_HOME = Path(tempfile.mkdtemp(prefix="agent-kit-tests-home-"))
atexit.register(shutil.rmtree, FAKE_HOME, True)

# Every ambient input that would otherwise decide a test's answer. Each is
# either redirected into FAKE_HOME (a location) or cleared (a value), never
# left to the machine.
#
# Cleared rather than redirected: these select or override, so any value at all
# is a decision the test did not make. A suite that wants one sets it itself.
_CLEARED = (
    # Identity and routing — agent-kit#285. WITAN_TARGET and WITAN_REMOTE_URL
    # decide local-vs-deployed, WITAN_ACTOR decides who owns a branch view.
    "WITAN_ACTOR",
    "WITAN_TARGET",
    "WITAN_REMOTE_URL",
    "WITAN_OIDC_ISSUER",
    "WITAN_OIDC_AUDIENCE",
    "WITAN_OIDC_CLIENT_ID",
    "WITAN_ACTOR_TOKENS_FILE",
    "WITAN_MEMORY_TOKEN",
    "WITAN_MEMORY_GRAPH",
    "WITAN_CODE_TOKEN",
    "WITAN_CODE_SERVER",
    "WITAN_CODE_TRANSPORT",
    "WITAN_CODE_INDEX_ROLE",
    "WITAN_REPO",
    "WITAN_AGENT",
    "WITAN_MODEL",
    "WITAN_AUTHOR",
    "WITAN_CONTEXT_TTL",
    "WITAN_REQUIRE_OMNIGRAPH",
    "WITAN_TEST_OMNIGRAPH_SERVER",
    "WITAN_TEST_OMNIGRAPH_GRAPH",
    "AC_KIT_CONFIG",
    # Observability: an exporter endpoint set on a developer's box would have
    # the suite emit spans at a real collector.
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "SENTRY_DSN",
)


def _redirect() -> None:
    home = FAKE_HOME
    (home / ".local" / "bin").mkdir(parents=True, exist_ok=True)

    for var in _CLEARED:
        os.environ.pop(var, None)

    # HOME itself, for the `Path.home()` reads no env var covers —
    # agent-config-kit's registry reaches for ~/.claude.json, ~/.claude/,
    # ~/.pi/ and ~/.config/opencode directly.
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)  # Windows' equivalent
    os.environ["XDG_CONFIG_HOME"] = str(home / ".config")
    os.environ["XDG_CACHE_HOME"] = str(home / ".cache")
    os.environ["XDG_DATA_HOME"] = str(home / ".local" / "share")

    # The state files whose defaults are module-level `Path.home()` constants
    # (witan_core.config_file, witan_core.remote.oidc). Moving HOME is not
    # enough for those: the constant is evaluated when the module is imported,
    # which for a test that imports it from another conftest can precede this.
    # Both consult their env var first, so setting it wins whatever the
    # constant froze.
    os.environ["WITAN_CONFIG"] = str(home / ".config" / "witan" / "config.toml")
    os.environ["WITAN_TOKEN_CACHE"] = str(home / ".config" / "witan" / "tokens.json")
    os.environ["WITAN_MERGE_WATERMARKS"] = str(
        home / ".config" / "witan" / "merge-watermarks.json"
    )
    # The graph stores. WITAN_MEMORY_URI is what `witan.server` bootstraps at
    # import; without it that import creates a real graph under the (now fake)
    # home, which is harmless but slow, and names the store `graph.omni` where
    # a suite may expect otherwise.
    os.environ["WITAN_MEMORY_URI"] = str(home / "graph.omni")
    os.environ["WITAN_CODE_DIR"] = str(home / "code")

    # Deterministic rendering, and WIDE. Rich wraps to the terminal, so a
    # message that fits on one line for the author wraps mid-phrase for a
    # reviewer with a narrower window, and an `in` assertion against it fails on
    # the inserted newline. That is how two agent-config-kit tests came to
    # assert a terminal width.
    #
    # Generous rather than merely fixed, because the wrap point is
    # data-dependent as well: these messages interpolate a `tmp_path`, whose
    # length varies per run, so a narrow width makes the suite depend on how
    # deep pytest's temp directory happened to be. 200 clears every message the
    # suite asserts on today with room to spare, while staying narrow enough
    # that pytest's own output is still readable in a CI log — which a width of
    # 1000 is not.
    #
    # ★ A width is headroom, not a guarantee. A test asserting a substring of a
    # long interpolated string is fragile at ANY finite width; the fix for that
    # test is to normalise whitespace before the check, not to widen this.
    #
    # ★ This pins what the suite asserts (message CONTENT) and says nothing
    # about whether the CLI renders READABLY in a narrow terminal. That is a
    # question about the product, filed separately; do not read a green suite as
    # having answered it.
    os.environ["COLUMNS"] = "200"
    os.environ["LINES"] = "40"

    # git needs an identity to commit, and the fake home has no ~/.gitconfig.
    # Supplying one keeps the many tests that build throwaway repos working
    # without each having to pass `-c user.email=...`.
    os.environ.setdefault("GIT_AUTHOR_NAME", "agent-kit tests")
    os.environ.setdefault("GIT_AUTHOR_EMAIL", "tests@example.invalid")
    os.environ.setdefault("GIT_COMMITTER_NAME", "agent-kit tests")
    os.environ.setdefault("GIT_COMMITTER_EMAIL", "tests@example.invalid")

    # ★ The one thing deliberately NOT isolated — see the module docstring.
    real_bin = REAL_HOME / ".local" / "bin"
    if real_bin.is_dir():
        os.environ["PATH"] = f"{real_bin}{os.pathsep}{os.environ.get('PATH', '')}"


_redirect()


# ── The guard ────────────────────────────────────────────────────────────────
#
# Redirecting the environment fixes today's leaks; it does not stop tomorrow's.
# A new package without the rootdir conftest, or anything that resolves a real
# path itself rather than through the env, walks straight past everything above
# — and the failure is silent, which is the whole reason this class survived
# three separate discoveries.
#
# So: watch the real home's state directories for the length of the session and
# say something if they grow. Two listdirs, no measurable cost, and it runs
# inside the jobs that already exist rather than adding one that re-runs every
# suite.

STRICT_ENV_VAR = "AGENT_KIT_STRICT_HERMETICITY"
"""Override the strictness this would otherwise infer. ``1`` on, ``0`` off.

Strict means a detected leak fails the run instead of warning. The default is
inferred from ``CI`` rather than set per-workflow: every runner exports it, so
strictness lands in all four test workflows AND the five publish ones AND
whatever gets added next, with no list to keep in sync. Ten `env:` blocks would
have been ten chances to forget the tenth.

Warn-only off CI, because a developer's machine can *legitimately* be writing to
these directories while the suite runs — another agent session indexing a repo,
a ``witan`` command in the next terminal — and a false failure people learn to
re-run past is worth less than no check at all. A runner has no concurrent
writer and no reason for these directories to gain anything, so there it is
strict.
"""


def _strict() -> bool:
    override = os.environ.get(STRICT_ENV_VAR)
    if override is not None:
        return override == "1"
    return bool(os.environ.get("CI"))


_WATCHED = (
    REAL_HOME / ".local" / "share" / "witan",
    REAL_HOME / ".config" / "witan",
    REAL_HOME / ".claude",
    REAL_HOME / ".pi",
)


def _entries(directory: Path) -> set[str]:
    """Top-level names under ``directory``, or empty if it does not exist."""
    try:
        return {p.name for p in directory.iterdir()}
    except OSError:
        return set()


_BEFORE = {d: _entries(d) for d in _WATCHED}


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 — pytest hook
    """Report anything the suite added to the real home."""
    leaked = {
        directory: sorted(_entries(directory) - before)
        for directory, before in _BEFORE.items()
        if _entries(directory) - before
    }
    if not leaked:
        return

    lines = [
        "",
        "TEST ENVIRONMENT LEAK — the suite wrote to the real home directory.",
        "",
        "  Something resolved a real path instead of the redirected one. See",
        "  testsupport/hermetic.py; the usual cause is a module-level default",
        "  captured before the redirection, or a package missing its rootdir",
        "  conftest.py.",
        "",
    ]
    for directory, names in leaked.items():
        lines.append(f"  {directory}")
        lines.extend(f"    + {name}" for name in names)
    lines.append("")

    if _strict():
        lines.append("  Failing the run.")
        print("\n".join(lines))
        session.exitstatus = 1
        return
    lines.append(
        f"  Warning only, off CI. Set {STRICT_ENV_VAR}=1 to make this fail here too."
    )
    print("\n".join(lines))
