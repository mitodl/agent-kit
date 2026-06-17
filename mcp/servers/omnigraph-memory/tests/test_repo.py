"""Unit tests for repo-slug normalisation (no omnigraph binary required)."""

import shutil
import subprocess

import pytest

from omnigraph_memory import repo


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:mitodl/ol-django.git", "https://github.com/mitodl/ol-django"),
        ("https://github.com/mitodl/ol-django", "https://github.com/mitodl/ol-django"),
        (
            "https://github.com/mitodl/ol-django.git",
            "https://github.com/mitodl/ol-django",
        ),
        ("git@gitlab.com:grp/sub/repo.git", "https://gitlab.com/grp/sub/repo"),
        (
            "https://x-token@github.com/mitodl/repo.git",
            "https://github.com/mitodl/repo",
        ),
        ("ssh://git@github.com/mitodl/repo.git", "https://github.com/mitodl/repo"),
    ],
)
def test_normalise_to_https_uri(url, expected):
    assert repo._normalise(url) == expected


def test_detect_override_wins(monkeypatch):
    monkeypatch.setenv("OMNIGRAPH_MEMORY_REPO", "https://env/repo")
    assert repo.detect(override="https://override/repo") == "https://override/repo"


def test_detect_env_fallback(monkeypatch):
    monkeypatch.setenv("OMNIGRAPH_MEMORY_REPO", "https://env/repo")
    assert repo.detect() == "https://env/repo"


def test_detect_empty_override_means_no_scope(monkeypatch):
    # An explicit empty string is "all repos" (None), overriding env/git.
    monkeypatch.setenv("OMNIGRAPH_MEMORY_REPO", "https://env/repo")
    assert repo.detect(override="") is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_detect_tolerates_multivalued_fetch(tmp_path, monkeypatch):
    # git allows several `fetch =` lines under a remote; configparser rejects
    # them (DuplicateOptionError). Detection must still resolve via git.
    monkeypatch.delenv("OMNIGRAPH_MEMORY_REPO", raising=False)
    monkeypatch.chdir(tmp_path)
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
    assert repo.detect() == "https://github.com/mitodl/ol-data-platform"
