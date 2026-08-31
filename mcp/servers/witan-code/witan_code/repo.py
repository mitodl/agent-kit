import configparser
import os
import re
import subprocess
from pathlib import Path

from witan_core.repo_key import find_git_config, normalise


def detect(override: str | None = None, start: Path | None = None) -> str | None:
    """
    Return a canonical repo key (HTTPS URI) for ``start`` (or the cwd).

    Resolution order:
      1. ``override`` parameter (explicit caller value) — canonicalized the
         same way an auto-detected remote is (see ``normalise``); an explicit
         empty string means "no repo scope"
      2. ``WITAN_REPO`` environment variable — canonicalized the same way; an
         explicit empty string also disables auto-detection here, matching
         witan-council's ``repo.detect`` (e.g. set in a global MCP server
         config where the server CWD is not the session's repo)
      3. ``git remote get-url origin`` (handles worktrees and multi-valued
         config keys that ``configparser`` rejects)
      4. ``origin`` remote URL parsed from the nearest ``.git/config``
      5. directory name of the git root (fallback when no remote)
      6. ``None`` — no repo context available
    """
    if override is not None:
        return normalise(override) if override else None

    env_repo = os.environ.get("WITAN_REPO")
    if env_repo is not None:
        return normalise(env_repo) if env_repo else None

    base = start or Path.cwd()

    if url := _git_origin(base):
        return normalise(url)

    git_config_path = find_git_config(base)
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


def git_toplevel(start: Path) -> Path | None:
    """The working-tree root containing ``start``, or ``None`` if it is not one.

    Asked of git for the same reason as :func:`_git_origin`: in a linked
    worktree ``.git`` is a file, so walking up looking for a ``.git``
    *directory* finds the wrong root or none at all.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    return Path(top) if top else None


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

    return normalise(parser.get(section, "url"))


# Omnigraph scratch branch for detached-HEAD checkouts: never index a detached
# state onto main (it would overwrite the shared view with an arbitrary commit).
DETACHED_BRANCH = "_detached"

_DEFAULT_BRANCHES = frozenset({"main", "master"})


def sanitize_branch(name: str) -> str:
    """Make a git branch name safe as an omnigraph branch name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or DETACHED_BRANCH


def branch_store_name(name: str) -> str:
    """Omnigraph branch name for a NON-DEFAULT git branch.

    Omnigraph reserves ``main`` for the store's default branch, so a feature
    branch literally named ``main`` (possible when the repo's default is
    ``master``) maps to ``_main``. sanitize_branch strips leading
    underscores, so no other git branch can produce a ``_``-prefixed name —
    the underscore namespace (``_main``, ``_detached``) is collision-free.
    """
    sanitized = sanitize_branch(name)
    return "_main" if sanitized == "main" else sanitized


def current_branch(start: Path | None = None) -> str | None:
    """Current git branch name for ``start`` (or the cwd).

    Returns ``"HEAD"`` for a detached checkout (git's own convention) and
    ``None`` outside a git repository or when git is unavailable.
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
    return result.stdout.strip() or None


def _origin_default_branch(base: Path) -> str | None:
    """Short name of origin's HEAD branch (``main``), if resolvable locally."""
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(base),
                "symbolic-ref",
                "--short",
                "refs/remotes/origin/HEAD",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    ref = result.stdout.strip()
    return (ref.partition("/")[2] or None) if ref else None


def _default_branch(base: Path) -> str | None:
    """The repo's default branch: origin HEAD, else main/master by presence.

    The local fallback prefers ``main`` over ``master`` when both exist so
    the choice is deterministic; whichever loses maps to its own store branch
    (collision-free either way). Returns None when no default is
    recognizable — then every branch gets its own store branch and nothing
    claims the store's main.
    """
    if default := _origin_default_branch(base):
        return default
    try:
        result = subprocess.run(
            ["git", "-C", str(base), "branch", "--list", *_DEFAULT_BRANCHES],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    present = {line.lstrip("*+ ").strip() for line in result.stdout.splitlines()}
    for name in ("main", "master"):
        if name in present:
            return name
    return None


def store_branch(start: Path | None = None) -> str | None:
    """Omnigraph branch for the checkout at ``start``: ``None`` = store main.

    The repo's default branch maps to the store's main branch; any other
    branch maps through ``branch_store_name`` (so a non-default branch named
    ``main`` gets ``_main``, never colliding with the store's default); a
    detached HEAD maps to the ``_detached`` scratch branch.
    """
    base = start or Path.cwd()
    branch = current_branch(base)
    if branch is None:
        return None
    if branch == "HEAD":
        return DETACHED_BRANCH
    if branch == _default_branch(base):
        return None
    return branch_store_name(branch)


def local_branches(start: Path | None = None) -> frozenset[str] | None:
    """Store-branch names of all local git branches, or None when git fails.

    Uses the same mapping as ``store_branch`` (``branch_store_name``) so
    ``branches --prune`` compares like with like — a git branch named
    ``main`` yields ``_main`` here, protecting its store branch from pruning.
    """
    base = start or Path.cwd()
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(base),
                "for-each-ref",
                "--format=%(refname:short)",
                "refs/heads",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return frozenset(
        branch_store_name(line) for line in result.stdout.splitlines() if line.strip()
    )


def root(start: Path | None = None) -> Path | None:
    """Return the git repository root for ``start`` (or the cwd).

    Prefers ``git rev-parse --show-toplevel`` so worktrees and submodules (where
    ``.git`` is a file, not a directory) resolve correctly; falls back to walking
    for ``.git/config`` when the git binary is unavailable.
    """
    base = start or Path.cwd()
    try:
        result = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
    except OSError:
        pass

    git_config = find_git_config(base)
    if git_config is None:
        return None
    return git_config.parent.parent
