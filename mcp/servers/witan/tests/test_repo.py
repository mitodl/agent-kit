"""Unit tests for repo-slug normalisation (no omnigraph binary required)."""

import shutil
import subprocess

import pytest

from witan import repo


# The repo-key canonicalizer (normalise) lives in witan_core.repo_key; its
# golden contract table is packages/witan-core/tests/test_repo_key.py.


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
def test_current_branch_falls_back_to_claude_project_dir(tmp_path, monkeypatch):
    """No explicit ``start`` and a cwd unrelated to the project (the
    persistent/global MCP server mode — same rationale as detect()'s
    WITAN_REPO escape hatch, but there's no analogous branch override) must
    still resolve via CLAUDE_PROJECT_DIR rather than reading the server
    process's own unrelated cwd."""
    base = _git_repo(tmp_path / "r")
    _git(base, "checkout", "-q", "-b", "feature/global-server")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(base))
    monkeypatch.chdir(tmp_path)  # cwd is NOT the project — no local git repo here
    assert repo.current_branch() == "feature/global-server"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_current_branch_explicit_start_wins_over_claude_project_dir(
    tmp_path, monkeypatch
):
    other = _git_repo(tmp_path / "other")
    base = _git_repo(tmp_path / "r")
    _git(base, "checkout", "-q", "-b", "feature/explicit")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(other))
    assert repo.current_branch(base) == "feature/explicit"


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


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_detect_falls_back_to_first_remote_when_no_origin(tmp_path, monkeypatch):
    """A repo whose only remote is not named ``origin`` still gets context."""
    monkeypatch.delenv("WITAN_REPO", raising=False)
    monkeypatch.chdir(tmp_path)
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "remote", "add", "upstream", "git@github.com:mitodl/upstreamed.git")
    assert repo.detect() == "https://github.com/mitodl/upstreamed"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_detect_prefers_origin_over_other_remotes(tmp_path, monkeypatch):
    """When both exist, ``origin`` still wins over a differently-named remote."""
    monkeypatch.delenv("WITAN_REPO", raising=False)
    monkeypatch.chdir(tmp_path)
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "remote", "add", "upstream", "git@github.com:other/fork.git")
    _git(tmp_path, "remote", "add", "origin", "git@github.com:mitodl/canonical.git")
    assert repo.detect() == "https://github.com/mitodl/canonical"


def test_parse_remote_falls_back_to_first_remote(tmp_path):
    """The git-binary-unavailable path (.git/config parse) also falls back
    past ``origin`` to the first remote carrying a url."""
    cfg = tmp_path / "config"
    cfg.write_text('[remote "fork"]\n\turl = git@github.com:mitodl/via-config.git\n')
    assert repo._parse_remote(cfg) == "https://github.com/mitodl/via-config"


def test_parse_remote_fallback_sorted_matches_git_order(tmp_path):
    """With multiple non-origin remotes, the config-parse fallback picks the
    alphabetically-first remote — matching `git remote`'s sorted order, so both
    detection paths agree regardless of section order in the file."""
    cfg = tmp_path / "config"
    cfg.write_text(
        '[remote "zeta"]\n\turl = git@github.com:mitodl/zeta.git\n'
        '[remote "alpha"]\n\turl = git@github.com:mitodl/alpha.git\n'
    )
    assert repo._parse_remote(cfg) == "https://github.com/mitodl/alpha"
