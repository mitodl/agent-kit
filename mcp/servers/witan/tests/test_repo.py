"""Unit tests for repo-slug normalisation (no omnigraph binary required)."""

import shutil
import subprocess

import pytest

from witan import repo


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
    monkeypatch.setenv("WITAN_REPO", "https://env/repo")
    assert repo.detect(override="https://override/repo") == "https://override/repo"


def test_detect_env_fallback(monkeypatch):
    monkeypatch.setenv("WITAN_REPO", "https://env/repo")
    assert repo.detect() == "https://env/repo"


def test_detect_empty_override_means_no_scope(monkeypatch):
    # An explicit empty string is "all repos" (None), overriding env/git.
    monkeypatch.setenv("WITAN_REPO", "https://env/repo")
    assert repo.detect(override="") is None


def _git(base, *args):
    subprocess.run(
        ["git", "-C", str(base), *args], check=True, capture_output=True, text=True
    )


def _git_repo(path):
    path.mkdir(exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(
        path,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "--allow-empty",
        "-q",
        "-m",
        "init",
    )
    return path


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_current_branch_returns_raw_unsanitized_name(tmp_path):
    """witan-code's sanitize_branch() would collapse the "/" to "_"
    (omnigraph branch names can't contain it) — witan's raw detector must
    keep it as-is, since the raw git name is the shared vocabulary between
    the two packages (schema.pg § Code Branches)."""
    base = _git_repo(tmp_path / "r")
    _git(base, "checkout", "-q", "-b", "feature/new-api")
    assert repo.current_branch(base) == "feature/new-api"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_current_branch_detached_head_is_none(tmp_path):
    base = _git_repo(tmp_path / "r")
    _git(base, "checkout", "-q", "--detach")
    assert repo.current_branch(base) is None


def test_current_branch_outside_git_is_none(tmp_path):
    assert repo.current_branch(tmp_path) is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_detect_tolerates_multivalued_fetch(tmp_path, monkeypatch):
    # git allows several `fetch =` lines under a remote; configparser rejects
    # them (DuplicateOptionError). Detection must still resolve via git.
    monkeypatch.delenv("WITAN_REPO", raising=False)
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
