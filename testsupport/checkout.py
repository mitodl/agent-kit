"""Git checkouts for tests to index or run against.

No pytest here, so a seeding script run as its own process (the editor checks'
``seed.py``) builds its checkouts the same way the suites do.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def git(path: Path, *args: str) -> None:
    """Run git in ``path`` as a fixed committer, so a commit needs no
    identity from the (hermetic, empty) global config."""
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    )


def git_checkout(
    path: Path,
    *,
    branch: str = "main",
    remote: str | None = None,
    files: dict[str, str] | None = None,
) -> Path:
    """A checkout at ``path`` on ``branch`` with one commit of ``files``
    (repo-relative path to text; an empty commit without them), and
    ``remote`` as its origin when given."""
    for rel, text in (files or {}).items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", branch)
    if remote:
        git(path, "remote", "add", "origin", remote)
    git(path, "add", "-A")
    git(path, "commit", "-q", "--allow-empty", "-m", "seed")
    return path
