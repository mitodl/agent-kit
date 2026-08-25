"""Tests for the workspace's shared test-environment guard.

``testsupport.hermetic`` is loaded by every package's rootdir ``conftest.py``,
so a guard that silently stopped guarding would take all five suites with it —
and by construction the symptom is nothing happening. Hence a test.

It lives in witan-core's suite rather than a suite of its own because
``testsupport`` is a repo-root module, not a package: it has no ``testpaths`` of
its own for pytest to find, and adding one would mean a sixth workspace package
with a version, a CHANGELOG and a release workflow for thirty lines of test
support. witan-core is the package everything else already depends on, which
makes it the least arbitrary of the five homes available.

The leak path itself is deliberately NOT exercised here: proving the detector
fires means writing to the real home, which is the one thing this whole
mechanism exists to prevent. That end of it was verified once, by hand, with a
throwaway test that wrote a sentinel into ``~/.local/share/witan`` — the guard
reported it and exited 1 under the strict flag, 0 with a warning without it.
What is asserted here is the logic that decision rests on.
"""

import os
from pathlib import Path

import pytest

from testsupport import hermetic


def test_the_redirection_actually_moved_the_home():
    """The premise every other suite is relying on."""
    assert Path.home() == hermetic.FAKE_HOME
    assert Path.home() != hermetic.REAL_HOME
    assert os.environ["HOME"] == str(hermetic.FAKE_HOME)


def test_the_state_files_point_inside_the_fake_home():
    for var in (
        "WITAN_CONFIG",
        "WITAN_TOKEN_CACHE",
        "WITAN_MERGE_WATERMARKS",
        "WITAN_MEMORY_URI",
        "WITAN_CODE_DIR",
    ):
        assert str(hermetic.FAKE_HOME) in os.environ[var], var


def test_the_ambient_selectors_are_cleared():
    """A value here is a decision the test did not make — agent-kit#285."""
    for var in hermetic._CLEARED:
        assert var not in os.environ, var


def test_the_real_local_bin_stays_on_path():
    """The one deliberate hole: omnigraph is a tool, not state.

    CI installs it to the real ``~/.local/bin``, so moving HOME without this
    would break every test that needs a graph.
    """
    real_bin = hermetic.REAL_HOME / ".local" / "bin"
    if not real_bin.is_dir():
        pytest.skip("no ~/.local/bin on this machine to keep reachable")
    assert str(real_bin) in os.environ["PATH"]


def test_marker_reports_a_missing_path_rather_than_raising():
    """The watched paths are optional — a fresh machine has none of them."""
    assert hermetic._marker(hermetic.FAKE_HOME / "nope" / "still-nope") == "<gone>"


def test_marker_changes_when_a_file_changes(tmp_path):
    """The agent-kit#282 leak APPENDED to a file that already existed. A
    name-based check cannot see that however deep it walks, so the marker
    carries size and mtime."""
    target = tmp_path / "merge-watermarks.json"
    target.write_text('{"a": 1}')
    before = hermetic._marker(target)

    target.write_text('{"a": 1, "leaked": 2}')

    assert hermetic._marker(target) != before


def test_marker_distinguishes_a_directory():
    assert hermetic._marker(hermetic.FAKE_HOME) == "dir"


def test_the_watch_reaches_deep_enough_for_a_nested_store():
    """The regression the shallow check had.

    The leak this whole change exists to stop lands at
    ``~/.local/share/witan/code/<slug>.omni`` — below a ``code`` directory that
    already exists on any machine that has run the indexer. Comparing immediate
    children saw no new name and reported nothing, on exactly the machines
    where the leak was real.
    """
    watched = dict(hermetic._WATCHED_TREES)
    store_root = hermetic.REAL_HOME / ".local" / "share" / "witan"

    assert watched[store_root] >= 3, (
        "depth must reach witan/code/<store>.omni/<file>, or the guard is "
        "blind to the defect it was written for"
    )


def test_the_watched_files_include_the_282_watermark():
    names = {path.name for path in hermetic._WATCHED_FILES}
    assert "merge-watermarks.json" in names


def test_an_exempt_selector_is_never_also_cleared():
    """Clearing WITAN_REQUIRE_OMNIGRAPH silently retired the binary-contract
    suite, whose entire purpose is to not be retired silently. The module
    asserts this at import; this states it as a test so the reason is findable.
    """
    assert not set(hermetic._CLEARED) & set(hermetic._EXEMPT)


def test_the_ci_switch_treats_an_explicit_negative_as_off(monkeypatch):
    """`CI=false` and `CI=0` are both set by real tooling."""
    monkeypatch.delenv(hermetic.STRICT_ENV_VAR, raising=False)
    for value, expected in (
        ("true", True),
        ("1", True),
        ("false", False),
        ("0", False),
        ("", False),
    ):
        monkeypatch.setenv("CI", value)
        assert hermetic._strict() is expected, value


def test_the_override_beats_the_inferred_default(monkeypatch):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv(hermetic.STRICT_ENV_VAR, "0")
    assert hermetic._strict() is False

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv(hermetic.STRICT_ENV_VAR, "1")
    assert hermetic._strict() is True


def test_ambient_churn_is_filtered_but_only_for_modifications():
    """A working machine rewrites these on its own schedule.

    Measured rather than guessed: a 95-second idle probe with no tests running
    reported a store's `.repo` sidecar, and a parallel `just test-all` added
    the token cache and Claude Code's own config. Four of five suites warned,
    every line of it ambient — and a check that cries wolf gets re-run past.
    """
    assert hermetic._is_ambient("/home/x/.claude.json")
    assert hermetic._is_ambient("/home/x/.config/witan/tokens.json")
    assert hermetic._is_ambient("/home/x/.local/share/witan/code/foo.omni.repo")
    assert hermetic._is_ambient("/home/x/.local/share/witan/graph.omni.lock")


def test_the_282_watermark_is_not_treated_as_ambient():
    """The append-to-an-existing-file leak has to stay visible: it only
    changes when someone actually runs `witan migrate merge`."""
    assert not hermetic._is_ambient("/home/x/.config/witan/merge-watermarks.json")


def test_a_store_directory_itself_is_not_ambient():
    """The sidecar churns; the store landing there is the leak."""
    assert not hermetic._is_ambient("/home/x/.local/share/witan/code/foo.omni")
