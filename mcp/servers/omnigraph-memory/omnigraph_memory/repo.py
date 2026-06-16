import configparser
import os
import re
from pathlib import Path


def detect(override: str | None = None) -> str | None:
    """
    Return a canonical repo slug for the current working directory.

    Resolution order:
      1. ``override`` parameter (explicit caller value)
      2. ``OMNIGRAPH_MEMORY_REPO`` environment variable
      3. ``origin`` remote URL from the nearest ``.git/config``
      4. ``None`` — no repo context available
    """
    if override:
        return override

    if env_repo := os.environ.get("OMNIGRAPH_MEMORY_REPO"):
        return env_repo

    git_config_path = _find_git_config(Path.cwd())
    if git_config_path is None:
        return None

    return _parse_origin(git_config_path)


def _find_git_config(start: Path) -> Path | None:
    """Walk up from ``start`` until a .git/config is found."""
    for directory in [start, *start.parents]:
        candidate = directory / ".git" / "config"
        if candidate.exists():
            return candidate
    return None


def _parse_origin(git_config: Path) -> str | None:
    """
    Parse .git/config and return the normalised ``origin`` remote URL.

    Uses configparser; falls back to None if the file is malformed or
    ``remote "origin"`` is absent.
    """
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
    Normalise a git remote URL to a canonical slug.

    Examples
    --------
    git@github.com:mitodl/ol-django.git  →  github.com/mitodl/ol-django
    https://github.com/mitodl/ol-django  →  github.com/mitodl/ol-django
    https://github.com/mitodl/repo.git   →  github.com/mitodl/repo
    """
    # Strip trailing .git
    url = re.sub(r"\.git$", "", url)

    # SSH: git@host:org/repo
    if m := re.match(r"git@([^:]+):(.+)", url):
        return f"{m.group(1)}/{m.group(2)}"

    # HTTPS / HTTP: https://host/org/repo
    if m := re.match(r"https?://([^/]+)/(.+)", url):
        return f"{m.group(1)}/{m.group(2)}"

    # Unknown format — return as-is
    return url
