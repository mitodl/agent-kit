import configparser
import os
import re
import subprocess
from pathlib import Path


def detect(override: str | None = None, start: Path | None = None) -> str | None:
    """
    Return a canonical repo key (HTTPS URI) for ``start`` (or the cwd).

    Resolution order:
      1. ``override`` parameter (explicit caller value)
      2. ``OMNIGRAPH_CODEGRAPH_REPO`` environment variable
      3. ``git remote get-url origin`` (handles worktrees and multi-valued
         config keys that ``configparser`` rejects)
      4. ``origin`` remote URL parsed from the nearest ``.git/config``
      5. directory name of the git root (fallback when no remote)
      6. ``None`` — no repo context available
    """
    if override:
        return override

    if env_repo := os.environ.get("OMNIGRAPH_CODEGRAPH_REPO"):
        return env_repo

    base = start or Path.cwd()

    if url := _git_origin(base):
        return _normalise(url)

    git_config_path = _find_git_config(base)
    if git_config_path is None:
        return None

    if slug := _parse_origin(git_config_path):
        return slug

    # No origin remote — fall back to the repo root directory name.
    return git_config_path.parent.parent.name


def _git_origin(start: Path) -> str | None:
    """Resolve the ``origin`` remote URL via git itself.

    git is the only correct parser of its own config: it resolves git worktrees
    (where ``.git`` is a file) and tolerates multi-valued keys (e.g. several
    ``fetch =`` lines) that Python's ``configparser`` rejects.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _find_git_config(start: Path) -> Path | None:
    """Walk up from ``start`` until a .git/config is found."""
    for directory in [start, *start.parents]:
        candidate = directory / ".git" / "config"
        if candidate.exists():
            return candidate
    return None


def _parse_origin(git_config: Path) -> str | None:
    """Parse .git/config and return the normalised ``origin`` remote URL."""
    parser = configparser.RawConfigParser()
    try:
        parser.read(git_config)
    except configparser.Error:
        return None

    section = 'remote "origin"'
    if not parser.has_option(section, "url"):
        return None

    return _normalise(parser.get(section, "url"))


def _normalise(url: str) -> str:
    """
    Normalise a git remote URL to its canonical HTTPS project URI.

    Must match ``omnigraph_memory/repo.py::_normalise`` so the repo key — used
    in symbol ids (``repo#path::Name``) and the Layer-1 ``symbol_refs`` that point
    at them — is identical across both stores.

    Examples
    --------
    git@github.com:mitodl/ol-django.git  →  https://github.com/mitodl/ol-django
    https://github.com/mitodl/ol-django  →  https://github.com/mitodl/ol-django
    """
    url = re.sub(r"\.git$", "", url.strip()).rstrip("/")

    if m := re.match(r"(?:ssh://)?[^@]+@([^:/]+)[:/](.+)", url):
        return f"https://{m.group(1)}/{m.group(2)}"

    if m := re.match(r"https?://(?:[^@/]+@)?([^/]+)/(.+)", url):
        return f"https://{m.group(1)}/{m.group(2)}"

    return url


def root(start: Path | None = None) -> Path | None:
    """Return the git repository root for ``start`` (or the cwd)."""
    base = start or Path.cwd()
    git_config = _find_git_config(base)
    if git_config is None:
        return None
    return git_config.parent.parent
