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
    "WITAN_OPTIMIZE_INTERVAL",
    "AC_KIT_CONFIG",
    # Rendering and transport selectors. WITAN_OUTPUT_FORMAT is a cyclopts
    # `env_var` on both CLIs, so an ambient `json` turns every table command's
    # output into something no assertion here expects.
    "WITAN_OUTPUT_FORMAT",
    "WITAN_OMNIGRAPH_HTTP",
    "OMNIGRAPH_BEARER_TOKEN",
    # The write-path scanner. `WITAN_SCAN_ENABLED=false` is a documented
    # opt-out, and inheriting it would run the whole suite with the scanner
    # off — green, and testing something other than what ships.
    "WITAN_SCAN_ENABLED",
    "WITAN_SCAN_SECRET_ACTION",
    "WITAN_SCAN_PII_ACTION",
    "WITAN_SCAN_ENABLED_DETECTORS",
    "WITAN_SCAN_DISABLED_DETECTORS",
    "WITAN_SCAN_PLUGINS",
    "WITAN_SCAN_ALLOWLIST",
    # Identity of the calling agent session, which several code paths record
    # as provenance.
    "CLAUDE_SESSION_ID",
    # Observability: an exporter endpoint set on a developer's box would have
    # the suite emit spans at a real collector.
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "SENTRY_DSN",
)


# ★ DELIBERATELY NOT CLEARED, and re-adding any of these breaks a check
# silently — which is precisely the class of defect this module exists to end.
#
#   WITAN_REQUIRE_OMNIGRAPH      .github/workflows/witan-core-tests.yml sets
#                                this to "1" on the test step so that a missing
#                                omnigraph binary is a HARD FAILURE instead of a
#                                skip. test_binary_contract.py reads it at
#                                MODULE SCOPE, which runs after this plugin, so
#                                popping it silently reverted the entire binary
#                                contract suite to skipping green. That suite's
#                                whole purpose is to stop itself being retired
#                                quietly; clearing this retired it quietly.
#
#   WITAN_TEST_OMNIGRAPH_SERVER  The documented opt-in for the live-server
#   WITAN_TEST_OMNIGRAPH_GRAPH   tests (`WITAN_TEST_OMNIGRAPH_SERVER=<url>
#                                pytest -k live_server`). Their `skipif` is
#                                evaluated at import, after this runs, so
#                                clearing them made those tests permanently
#                                unreachable — you could not opt in at all.
#
# The distinction is whether the variable selects BEHAVIOUR THE SUITE SHOULD NOT
# INHERIT (clear it) or ENABLES A TEST MODE THE CALLER ASKED FOR (keep it). An
# ambient value that changes what the code under test does is contamination; an
# ambient value that a human or a workflow set to make the suite stricter is an
# instruction.
_EXEMPT = (
    "WITAN_REQUIRE_OMNIGRAPH",
    "WITAN_TEST_OMNIGRAPH_SERVER",
    "WITAN_TEST_OMNIGRAPH_GRAPH",
)
assert not set(_CLEARED) & set(_EXEMPT), "an exempt selector must not also be cleared"


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


def _falsey(value: str) -> bool:
    """Treat an explicitly-negative string as unset.

    ``CI=false`` and ``CI=0`` are both set by real tooling, and a bare
    truthiness check reads them as "yes, strict". Same parsing as
    ``test_binary_contract.py`` uses for ``WITAN_REQUIRE_OMNIGRAPH``.
    """
    return value.strip().lower() in ("", "0", "false", "no", "off")


def _strict() -> bool:
    override = os.environ.get(STRICT_ENV_VAR)
    if override is not None:
        return not _falsey(override)
    return not _falsey(os.environ.get("CI", ""))


# What to watch, and how deep. Depth matters more than it looks: the leak this
# whole change exists to stop lands at ``~/.local/share/witan/code/<slug>.omni``
# — INSIDE a ``code`` directory that already exists on any machine that has run
# the indexer. Comparing only immediate children of ``~/.local/share/witan``
# therefore saw no new name and reported nothing, on exactly the machines where
# the leak was real. It only ever fired on CI, where the whole tree is absent so
# ``code`` itself counts as new.
#
# Depth 3 covers store creation under ``code/`` and writes landing in an
# existing store's ``nodes``/``edges``/``__manifest`` directories.
#
# ★ A depth is a bound, not a proof. A write buried deeper than this goes
# unreported, and the check says so rather than implying it is exhaustive.
_WATCHED_TREES = (
    (REAL_HOME / ".local" / "share" / "witan", 3),
    (REAL_HOME / ".config" / "witan", 2),
)

# Watched as FILES, by size and mtime, because the agent-kit#282 leak was an
# APPEND to a file that already existed — a new-name check cannot see that, no
# matter how deep it walks.
_WATCHED_FILES = (
    REAL_HOME / ".config" / "witan" / "merge-watermarks.json",
    REAL_HOME / ".claude.json",
    REAL_HOME / ".claude" / "settings.json",
    REAL_HOME / ".pi" / "agent" / "mcp.json",
)


# Paths a working machine rewrites on its own schedule. Measured, not guessed:
# a 95-second idle probe (no tests running at all) and a parallel `just
# test-all` between them reported exactly these — the indexer touching a
# store's `.repo` sidecar, the OIDC client refreshing its token, Claude Code
# persisting its own config.
#
# Only their MODIFICATION is ignored, never their creation. On CI none of these
# exist, so a test that writes one still shows up as a `created` and is caught;
# locally they pre-exist, so the churn is a modification and is filtered. That
# keeps the agent-kit#282 shape — an append to an existing
# `merge-watermarks.json`, which is NOT on this list — visible where it happens.
#
# ★ Four of five suites warned before this existed, every line of it ambient.
# A check that cries wolf gets re-run past, which this file's own docstring says
# is worth less than no check; the filter is what makes the warning mean
# something off CI.
_AMBIENT_CHURN = (
    ".claude.json",  # Claude Code's own state, rewritten constantly
    "tokens.json",  # refreshed by any witan client, incl. another session
    "tokens.json.lock",
)
_AMBIENT_SUFFIXES = (
    ".omni.repo",  # per-store sidecar, touched on any index
    ".lock",
    ".schema_mtime",
)


def _is_ambient(path_str: str) -> bool:
    """Whether a MODIFICATION of this path is the machine rather than the suite."""
    name = path_str.rsplit("/", 1)[-1]
    return name in _AMBIENT_CHURN or path_str.endswith(_AMBIENT_SUFFIXES)


def _marker(path: Path) -> str:
    """A value that changes when ``path`` does. ``dir`` for directories.

    Size and mtime rather than a hash: these trees hold hundreds of megabytes
    of Lance data, and the check runs twice per session.
    """
    try:
        st = path.stat()
    except OSError:
        return "<gone>"
    if path.is_dir():
        return "dir"
    return f"{st.st_size}:{st.st_mtime_ns}"


def _snapshot() -> dict[str, str]:
    """Relative path -> marker, for everything currently watched."""
    seen: dict[str, str] = {}

    def walk(directory: Path, root: Path, depth: int) -> None:
        if depth < 0:
            return
        try:
            children = sorted(directory.iterdir())
        except OSError:
            return
        for child in children:
            # Absolute paths as keys, so a path reached by both the tree walk
            # and the explicit file list is one entry rather than two.
            seen[str(child)] = _marker(child)
            if child.is_dir():
                walk(child, root, depth - 1)

    for root, depth in _WATCHED_TREES:
        walk(root, root, depth - 1)
    for path in _WATCHED_FILES:
        if path.exists():
            seen[str(path)] = _marker(path)
    return seen


_BEFORE = _snapshot()


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 — pytest hook
    """Report anything the suite added to, or changed in, the real home."""
    now = _snapshot()
    added = sorted(k for k in now if k not in _BEFORE)
    changed = sorted(
        k for k in now if k in _BEFORE and now[k] != _BEFORE[k] and not _is_ambient(k)
    )
    if not added and not changed:
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
    lines.extend(f"    created  {name}" for name in added)
    lines.extend(f"    modified {name}" for name in changed)
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
