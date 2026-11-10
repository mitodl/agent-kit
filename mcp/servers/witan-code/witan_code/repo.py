import configparser
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from witan_core.repo_key import find_git_config, normalise


class RepoNotDetected(RuntimeError):
    """No stable repo key could be resolved for a path.

    Raised only by callers that would otherwise CREATE something keyed on the
    guess — see :func:`witan_code.indexer.index_path`. A directory name is not
    a repo key: it is neither unique (two ``/tmp/*/tests`` checkouts collide on
    ``tests``, silently overwriting each other's index) nor meaningful in a
    listing of real per-repo graphs.
    """


def detect(
    override: str | None = None,
    start: Path | None = None,
    *,
    dirname_fallback: bool = True,
) -> str | None:
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
      5. directory name of the git root — only when ``dirname_fallback``
      6. ``None`` — no repo context available

    ``dirname_fallback`` is on by default because reads of a LOCAL store want
    it: it is how a store that was already created under a bare directory name
    stays reachable. Write paths pass ``False``, because that same guess is
    what files a scratch directory permanently into the shared per-repo
    namespace. Reads that may target a cluster go through
    :func:`witan_code.store.detect_repo`, which also passes ``False`` there.
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
    return git_config_path.parent.parent.name if dirname_fallback else None


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


def unignored_files(
    directory: Path, *, on_error: Callable[[OSError], None] | None = None
) -> list[Path] | None:
    """Every path git lists under ``directory``: tracked, plus untracked but not
    ignored. ``None`` when git cannot answer (no git binary, or ``directory``
    is not inside a work tree), which callers take as "walk the disk instead".

    ``on_error`` gets anything git wrote to stderr on a run that otherwise
    succeeded. A directory git cannot open is one such warning, and git then
    leaves its untracked files out of the listing and still exits 0, the same
    silent shrink ``os.walk`` commits without an ``onerror``.

    Ignore rules are git's own (``.gitignore`` at every level,
    ``.git/info/exclude``, ``core.excludesFile``), so a virtualenv under any
    name is left out: uv and ``python -m venv`` both drop a ``*`` .gitignore
    inside the environment they create. A tracked file is listed even when a
    pattern matches it, as git itself treats it.

    The listing is what git knows, not what is on disk, so callers check
    existence: a tracked file deleted from the work tree is still listed, and
    so is a submodule's gitlink (a directory). git does not descend into a
    nested repository or a submodule; a directory that only looks like one (a
    ``.git`` entry git cannot open) is listed through, so callers keep their
    own nested-checkout check.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(directory),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    if result.stderr.strip() and on_error is not None:
        on_error(OSError(f"git ls-files: {os.fsdecode(result.stderr).strip()}"))
    # -z: no quoting, and a name may hold spaces or newlines. An unmerged path
    # is listed once per conflict stage, hence the dedupe.
    names = dict.fromkeys(name for name in result.stdout.split(b"\0") if name)
    return [directory / os.fsdecode(name) for name in names]


def ignored(repo_root: Path, paths: list[Path]) -> frozenset[Path]:
    """The members of ``paths`` (absolute, all inside ``repo_root``) that git
    ignores, asked in one ``git check-ignore`` call.

    Empty when git cannot answer, which leaves the caller indexing them as it
    did before this check existed. Tracked files are never reported, matching
    :func:`unignored_files`.
    """
    if not paths:
        return frozenset()
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "check-ignore", "-z", "--stdin"],
            input=b"\0".join(os.fsencode(path) for path in paths),
            capture_output=True,
            check=False,
        )
    except OSError:
        return frozenset()
    # 0: some path is ignored; 1: none is; anything else is a fatal error.
    if result.returncode not in (0, 1):
        return frozenset()
    return frozenset(
        Path(os.fsdecode(name)) for name in result.stdout.split(b"\0") if name
    )
