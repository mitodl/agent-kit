"""Indexing something with no resolvable repo key is refused, not guessed.

The guess used to be the target's directory name, written as a permanent store
into the SHARED per-repo store directory. That key is neither unique (two
``/tmp/*/tests`` layouts collide on ``tests``, one silently overwriting the
other) nor recognizable next to real repo graphs — 36 such stores accumulated on
one machine, named ``out2``, ``tmp``, ``data``, ``sf``.

No omnigraph binary needed: the refusal lands before the store is touched, which
is the whole point.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from witan_code import indexer
from witan_code import repo as repo_module


@pytest.fixture
def scratch(tmp_path, monkeypatch) -> Path:
    """A source tree with no repo key available from anywhere."""
    monkeypatch.delenv("WITAN_REPO", raising=False)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    src = tmp_path / "tests"
    src.mkdir()
    (src / "a.py").write_text("def a():\n    return 1\n")
    return src


def _code_dir(tmp_path: Path) -> list[Path]:
    code = tmp_path / "code"
    return sorted(code.iterdir()) if code.exists() else []


def test_bare_directory_is_refused_and_creates_no_store(scratch, tmp_path):
    with pytest.raises(repo_module.RepoNotDetected) as exc:
        indexer.index_path(scratch)

    assert "no git remote" in str(exc.value)
    assert "WITAN_REPO" in str(exc.value)
    assert _code_dir(tmp_path) == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_git_checkout_without_a_remote_is_refused_too(scratch, tmp_path):
    """The other vector: a real checkout whose ``origin`` was never added.
    ``detect`` resolves a git root here, so the refusal has to come from the
    absence of a *remote*, not the absence of git."""
    subprocess.run(["git", "init", "-q", str(scratch)], check=True)

    with pytest.raises(repo_module.RepoNotDetected):
        indexer.index_path(scratch)

    assert _code_dir(tmp_path) == []


def test_a_file_under_a_bare_directory_is_refused(scratch, tmp_path):
    """The PostToolUse reindex hook passes a single edited file, which is how
    scratch layouts got indexed without anyone running `index` at all."""
    with pytest.raises(repo_module.RepoNotDetected):
        indexer.index_path(scratch / "a.py")

    assert _code_dir(tmp_path) == []
