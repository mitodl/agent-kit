import configparser
import os
import re
import subprocess
from pathlib import Path

from witan_core.repo_key import find_git_config, normalise


def detect(override: str | None = None) -> str | None:
    """
    Return a canonical repo key for the current working directory.

    The key is the project's canonical HTTPS remote URI when a git remote is
    available (e.g. ``https://github.com/mitodl/ol-django``). This URI is the
    shared join key across every layer — memory, workflow, tasks, and the
    code graph — so they must all derive it the same way.

    Resolution order:
      1. ``override`` parameter — a non-empty value is canonicalized the same
         way an auto-detected remote is (see ``normalise``); an explicit empty
         string means "no repo scope" (callers use this for all-repos)
      2. ``WITAN_REPO`` environment variable — canonicalized the same way
      3. the ``origin`` remote URL from the nearest ``.git/config``, else the
         first remote of any name (a repo cloned/added under a different remote
         name still gets context)
      4. ``None`` — no repo context available
    """
    if override is not None:
        return normalise(override) if override else None

    env_repo = os.environ.get("WITAN_REPO")
    if env_repo is not None:
        # Empty string explicitly disables auto-detection (e.g. set in a global
        # MCP server config where the server CWD is not the session's repo).
        return normalise(env_repo) if env_repo else None

    cwd = Path.cwd()

    # git is the only correct parser of its own config: it resolves worktrees
    # (where .git is a file) and tolerates multi-valued keys (e.g. several
    # `fetch =` lines) that configparser rejects. Fall back to parsing
    # .git/config directly when the git binary is unavailable.
    if url := git_remote_url(cwd):
        return normalise(url)

    git_config_path = find_git_config(cwd)
    if git_config_path is None:
        return None

    return _parse_remote(git_config_path)


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
        if name == "origin":
            continue  # already tried above — don't spawn a redundant git call
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
        return normalise(parser.get('remote "origin"', "url"))

    # Match ``git_remote_url``'s fallback order: git lists remotes sorted by
    # name, so sort the candidate sections by remote name too. Otherwise the
    # two paths could pick different remotes (config-file order vs git's sorted
    # order), defeating the point of keeping them aligned.
    candidates = sorted(
        (
            (m.group(1), section)
            for section in parser.sections()
            if (m := re.fullmatch(r'remote "(.+)"', section))
            and parser.has_option(section, "url")
        ),
        key=lambda pair: pair[0],
    )
    if candidates:
        return normalise(parser.get(candidates[0][1], "url"))

    return None
