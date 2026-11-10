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


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_detect_falls_back_to_dirname_for_reads(tmp_path, monkeypatch):
    """A store already created under a bare directory name has to stay
    reachable, so the read default keeps the fallback."""
    monkeypatch.delenv("WITAN_REPO", raising=False)
    checkout = tmp_path / "scratchproj"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)

    assert repo.detect(start=checkout) == "scratchproj"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "scratchproj"),
        ({"WITAN_CODE_SERVER": "https://omnigraph.example"}, None),
        ({"WITAN_CODE_TRANSPORT": "mcp"}, None),
    ],
)
def test_detect_repo_only_falls_back_to_dirname_for_a_local_store(
    tmp_path, monkeypatch, env, expected
):
    """No cluster graph is keyed by a directory name, so a cluster read gets
    no repo rather than a probe of a graph that cannot exist."""
    from witan_code import store

    monkeypatch.delenv("WITAN_REPO", raising=False)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    checkout = tmp_path / "scratchproj"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    monkeypatch.chdir(checkout)

    assert store.detect_repo() == expected


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_detect_without_dirname_fallback_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("WITAN_REPO", raising=False)
    checkout = tmp_path / "scratchproj"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)

    assert repo.detect(start=checkout, dirname_fallback=False) is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_dirname_fallback_does_not_override_an_explicit_repo(tmp_path, monkeypatch):
    """The opt-in has to work on the write path: WITAN_REPO is resolved before
    the remote lookup, so `dirname_fallback=False` never reaches it."""
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/cg")
    checkout = tmp_path / "scratchproj"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)

    assert (
        repo.detect(start=checkout, dirname_fallback=False)
        == "https://github.com/test/cg"
    )


def _commit_all(checkout):
    subprocess.run(["git", "add", "-A"], cwd=checkout, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        cwd=checkout,
        check=True,
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_unignored_files_is_none_outside_a_repository(tmp_path):
    """No repository is the one case that sends a caller back to the walk."""
    assert repo.unignored_files(tmp_path) is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_a_repository_git_cannot_list_reports_and_lists_nothing(tmp_path):
    """A corrupt index must not fall back to the walk, which would index the
    ignored trees the listing exists to leave out."""
    checkout = tmp_path / "proj"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    (checkout / "a.py").write_text("x = 1\n")
    _commit_all(checkout)
    (checkout / ".git" / "index").write_bytes(b"not an index")
    errors = []

    assert repo.unignored_files(checkout, on_error=errors.append) == []
    assert len(errors) == 1
    assert "git ls-files failed" in str(errors[0])


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_a_tracked_directory_replaced_by_a_symlink_is_not_followed(tmp_path):
    """The index still names the old children; following the link would read
    another directory's files as this repository's. A symlink to a single file
    is kept."""
    checkout = tmp_path / "proj"
    (checkout / "pkg").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    (checkout / "pkg" / "a.py").write_text("x = 1\n")
    _commit_all(checkout)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.py").write_text("secret = 1\n")
    (outside / "b.py").write_text("y = 2\n")
    shutil.rmtree(checkout / "pkg")
    (checkout / "pkg").symlink_to(outside, target_is_directory=True)
    (checkout / "link.py").symlink_to(outside / "b.py")

    listed = repo.unignored_files(checkout)

    assert checkout / "pkg" / "a.py" not in listed
    assert checkout / "link.py" in listed
