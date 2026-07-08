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
      3. the ``origin`` remote URL from the nearest ``.git/config``, else the
         first remote of any name (a repo cloned/added under a different remote
         name still gets context)
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
    if url := git_remote_url(cwd):
        return _normalise(url)

    git_config_path = _find_git_config(cwd)
    if git_config_path is None:
        return None

    return _parse_remote(git_config_path)


def normalise(url: str) -> str:
    """Public alias for the canonical-URI normaliser (see ``_normalise``).

    Exposed so the context hook can share one normalisation path with
    ``detect`` instead of reimplementing it and drifting."""
    return _normalise(url)


def current_branch(start: Path | None = None) -> str | None:
    """Raw git branch name for ``start`` (or ``CLAUDE_PROJECT_DIR``, or the
    cwd), or ``None``.

    The ``CLAUDE_PROJECT_DIR`` fallback matters for a persistent/global witan
    MCP server (docs: ``WITAN_REPO`` — "set in a global MCP server config
    where the server CWD is not the session's repo"): ``detect()`` already
    has its own escape hatch for that mode via ``WITAN_REPO``, but there is
    no analogous "WITAN_BRANCH" override, so branch detection needs its own
    fallback to the session's actual project directory rather than silently
    reading whatever repo the server process happens to be sitting in (or
    finding no repo at all).

    Returns ``None`` outside a git repository, when git is unavailable, or
    for a detached HEAD checkout — CodeBranch tracks meaningful work
    branches, not arbitrary commits. Unlike witan-code's own
    ``current_branch()``/``store_branch()``, this is never sanitized: the
    raw name is the shared vocabulary between witan and witan-code (see
    ``CodeBranch`` in schema.pg); sanitizing for omnigraph-safe storage is a
    witan-code concern that must not leak here.
    """
    if start is not None:
        base = start
    elif project_dir := os.environ.get("CLAUDE_PROJECT_DIR"):
        base = Path(project_dir)
    else:
        base = Path.cwd()
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


def git_remote_url(start: Path) -> str | None:
    """Resolve a remote URL via git itself: ``origin`` if present, else the
    first remote of any name.

    Falling back past ``origin`` means a checkout whose canonical remote is
    named ``upstream``/``fork``/etc. (or a single differently-named remote)
    still yields repo context instead of silently getting none. Returns the
    raw URL; callers normalise it. Fault-tolerant: no git binary or no remote
    → ``None``."""
    if url := _git_remote_get_url(start, "origin"):
        return url
    for name in _git_remote_names(start):
        if url := _git_remote_get_url(start, name):
            return url
    return None


def _git_remote_get_url(start: Path, name: str) -> str | None:
    """Raw URL of remote ``name`` via ``git remote get-url``, or ``None``."""
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "remote", "get-url", name],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _git_remote_names(start: Path) -> list[str]:
    """Names of configured remotes (in git's stable, sorted order), or ``[]``."""
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "remote"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _find_git_config(start: Path) -> Path | None:
    """Walk up from ``start`` until a .git/config is found."""
    for directory in [start, *start.parents]:
        candidate = directory / ".git" / "config"
        if candidate.exists():
            return candidate
    return None


def _parse_remote(git_config: Path) -> str | None:
    """
    Parse .git/config and return the normalised remote URL: ``origin`` if it
    has a url, else the first ``remote "…"`` section that does.

    Uses configparser; falls back to None if the file is malformed or no
    remote section carries a url. Mirrors ``git_remote_url``'s origin-first,
    then-any-remote order for the git-binary-unavailable path.
    """
    parser = configparser.RawConfigParser()
    try:
        parser.read(git_config)
    except configparser.Error:
        return None

    if parser.has_option('remote "origin"', "url"):
        return _normalise(parser.get('remote "origin"', "url"))

    for section in parser.sections():
        if section.startswith("remote ") and parser.has_option(section, "url"):
            return _normalise(parser.get(section, "url"))

    return None


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
