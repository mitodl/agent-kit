"""Unit tests for codegraph repo detection (no binary required)."""

import shutil
import subprocess

import pytest

from witan_code import repo


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:mitodl/ol-django.git", "https://github.com/mitodl/ol-django"),
        ("https://github.com/mitodl/ol-django", "https://github.com/mitodl/ol-django"),
        ("ssh://git@github.com/mitodl/repo.git", "https://github.com/mitodl/repo"),
    ],
)
def test_normalise_matches_memory_layer(url, expected):
    # Must stay identical to witan.repo._normalise so symbol ids and
    # the Layer-1 symbol_refs that point at them share one repo key.
    assert repo._normalise(url) == expected


def test_detect_env_override(monkeypatch):
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/cg")
    assert repo.detect() == "https://github.com/test/cg"


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
