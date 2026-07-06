import configparser
import os
import re
import subprocess
from pathlib import Path


def detect(override: str | None = None) -> str | None:
    """
    Return a canonical repo key for the current working directory.

    The key is the project's canonical HTTPS remote URI when a git remote is
    available (e.g. ``https://github.com/mitodl/ol-django``). This URI is the
    shared join key across every layer — memory, workflow, tasks, and the
    code graph — so they must all derive it the same way.

    Resolution order:
      1. ``override`` parameter — a non-empty value is used as-is; an explicit
         empty string means "no repo scope" (callers use this for all-repos)
      2. ``WITAN_REPO`` environment variable
      3. ``origin`` remote URL from the nearest ``.git/config``
      4. ``None`` — no repo context available
    """
    if override is not None:
        return override or None

    env_repo = os.environ.get("WITAN_REPO")
    if env_repo is not None:
        # Empty string explicitly disables auto-detection (e.g. set in a global
        # MCP server config where the server CWD is not the session's repo).
        return env_repo or None

    cwd = Path.cwd()

    # git is the only correct parser of its own config: it resolves worktrees
    # (where .git is a file) and tolerates multi-valued keys (e.g. several
    # `fetch =` lines) that configparser rejects. Fall back to parsing
    # .git/config directly when the git binary is unavailable.
    if url := _git_origin(cwd):
        return _normalise(url)

    git_config_path = _find_git_config(cwd)
    if git_config_path is None:
        return None

    return _parse_origin(git_config_path)


def current_branch(start: Path | None = None) -> str | None:
    """Raw git branch name for ``start`` (or the cwd), or ``None``.

    Returns ``None`` outside a git repository, when git is unavailable, or
    for a detached HEAD checkout — CodeBranch tracks meaningful work
    branches, not arbitrary commits. Unlike witan-code's own
    ``current_branch()``/``store_branch()``, this is never sanitized: the
    raw name is the shared vocabulary between witan and witan-code (see
    ``CodeBranch`` in schema.pg); sanitizing for omnigraph-safe storage is a
    witan-code concern that must not leak here.
    """
    base = start or Path.cwd()
    try:
        result = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch if branch and branch != "HEAD" else None


def _git_origin(start: Path) -> str | None:
    """Resolve the ``origin`` remote URL via git itself."""
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
    Normalise a git remote URL to its canonical HTTPS project URI.

    Examples
    --------
    git@github.com:mitodl/ol-django.git  →  https://github.com/mitodl/ol-django
    https://github.com/mitodl/ol-django  →  https://github.com/mitodl/ol-django
    git@gitlab.com:grp/sub/repo.git      →  https://gitlab.com/grp/sub/repo
    """
    # Strip trailing .git and any auth userinfo in https remotes.
    url = re.sub(r"\.git$", "", url.strip()).rstrip("/")

    # SSH: git@host:org/repo  →  https://host/org/repo
    if m := re.match(r"(?:ssh://)?[^@]+@([^:/]+)[:/](.+)", url):
        return f"https://{m.group(1)}/{m.group(2)}"

    # HTTP(S): normalise scheme to https, drop any userinfo.
    if m := re.match(r"https?://(?:[^@/]+@)?([^/]+)/(.+)", url):
        return f"https://{m.group(1)}/{m.group(2)}"

    # Unknown format — return as-is.
    return url
