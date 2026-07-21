"""Unit tests for codegraph repo detection (no binary required)."""

import shutil
import subprocess

import pytest

from witan_code import repo


# The repo-key canonicalizer (normalise) lives in witan_core.repo_key — a single
# source of truth shared with the memory layer (no more "must stay identical"
# copy). Its golden contract table is packages/witan-core/tests/test_repo_key.py.


def test_detect_env_override(monkeypatch):
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/cg")
    assert repo.detect() == "https://github.com/test/cg"


def test_detect_env_override_is_normalised(monkeypatch):
    monkeypatch.setenv("WITAN_REPO", "https://GitHub.com/MITODL/OL-Django")
    assert repo.detect() == "https://github.com/mitodl/ol-django"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_detect_empty_env_disables_autodetection(monkeypatch, tmp_path):
    """An explicit empty WITAN_REPO must disable auto-detection outright,
    matching witan-council's repo.detect — not fall through to git remote
    lookup (the walrus-operator `if env_repo := ...` form treats "" as
    falsy and used to do exactly that)."""
    monkeypatch.setenv("WITAN_REPO", "")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            "git@github.com:mitodl/should-not-be-detected.git",
        ],
        check=True,
    )
    assert repo.detect(start=tmp_path) is None


def test_detect_override_arg_is_normalised(monkeypatch):
    monkeypatch.delenv("WITAN_REPO", raising=False)
    assert (
        repo.detect(override="git@github.com:MITODL/OL-Django.git")
        == "https://github.com/mitodl/ol-django"
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_detect_tolerates_multivalued_fetch(tmp_path, monkeypatch):
    # git allows several `fetch =` lines under a remote; configparser rejects
    # them (DuplicateOptionError). Detection must still resolve via git.
    monkeypatch.delenv("WITAN_REPO", raising=False)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            "git@github.com:mitodl/ol-data-platform.git",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "config",
            "--add",
            "remote.origin.fetch",
            "+refs/heads/release/*:refs/remotes/origin/release/*",
        ],
        check=True,
    )
    assert repo.detect(start=tmp_path) == "https://github.com/mitodl/ol-data-platform"
