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


def test_entries_is_empty_for_a_directory_that_is_not_there():
    """The watched directories are optional — a fresh machine has none."""
    assert hermetic._entries(hermetic.FAKE_HOME / "nope" / "still-nope") == set()


def test_entries_reports_top_level_names_only(tmp_path):
    (tmp_path / "a.omni").mkdir()
    (tmp_path / "a.omni" / "buried").write_text("x")
    (tmp_path / "b.json").write_text("x")

    assert hermetic._entries(tmp_path) == {"a.omni", "b.json"}


def test_a_new_entry_is_what_counts_as_a_leak(tmp_path):
    """Growth, not difference: a suite that DELETES something from the real
    home is a different (and louder) problem, and pre-existing entries must not
    register — a developer's machine is full of them."""
    (tmp_path / "pre-existing").write_text("x")
    before = hermetic._entries(tmp_path)

    assert hermetic._entries(tmp_path) - before == set()

    (tmp_path / "leaked").write_text("x")
    assert hermetic._entries(tmp_path) - before == {"leaked"}
