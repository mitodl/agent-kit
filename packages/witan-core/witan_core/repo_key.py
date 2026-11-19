"""Canonical repo-key derivation — the cross-layer join key.

``normalise`` turns any git remote URL into the canonical HTTPS project URI used
as the shared join key across every witan layer (memory, workflow, tasks, and
the code graph) and inside symbol ids (``repo_uri#path::Name``). Both servers
MUST derive it identically or those joins silently break — hence the single
source of truth here, guarded by ``tests/test_repo_key.py``'s golden table.

``detect``/``current_branch`` are intentionally NOT here: they diverge between
the two servers (witan keeps the raw branch; witan-code sanitizes it for
omnigraph-safe branch storage), so each keeps its own in ``repo.py``.
:func:`remote_uri` and :func:`checkout_branch` are the path-explicit pair the
language server uses per workspace folder; the branch is raw, and witan-code's
plugin sanitizes it the way its own ``repo.py`` does.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# Hosts whose org/repo path segments are known to be case-insensitive, so
# folding them to lowercase can't merge two genuinely distinct repos. GitHub
# and GitLab both fold org/repo names case-insensitively server-side. A
# generic/self-hosted git host may not (e.g. a case-sensitive filesystem
# backing bare repos), so the path is left as-is for anything not listed here
# — see issue #142's "Possible Solution" discussion.
_CASE_INSENSITIVE_PATH_HOSTS = frozenset({"github.com", "gitlab.com"})


def normalise(url: str) -> str:
    """Normalise a git remote URL to its canonical HTTPS project URI.

    The host is always lowercased (DNS hostnames are inherently
    case-insensitive). The org/repo path is additionally lowercased for hosts
    in :data:`_CASE_INSENSITIVE_PATH_HOSTS`, so ``Org/Repo`` and ``org/repo``
    on GitHub/GitLab always canonicalize to the same key — this is a golden
    contract: symbol ids and every ``repo``-keyed record join on this exact
    output, so changing it requires a data migration (``witan migrate
    repo-keys``), not just a code change.

    Examples
    --------
    git@github.com:mitodl/ol-django.git  →  https://github.com/mitodl/ol-django
    https://github.com/MITODL/OL-Django  →  https://github.com/mitodl/ol-django
    git@gitlab.com:grp/sub/repo.git      →  https://gitlab.com/grp/sub/repo
    https://Git.example.com/Org/Repo     →  https://git.example.com/Org/Repo
    """
    # Strip trailing slashes first, then a trailing .git, so a malformed
    # ".../repo.git/" canonicalizes to ".../repo" rather than leaving ".git"
    # stranded (the .git$ anchor won't match when a slash trails it).
    url = re.sub(r"\.git$", "", url.strip().rstrip("/"))

    # SSH: git@host:org/repo  →  https://host/org/repo
    if m := re.match(r"(?:ssh://)?[^@]+@([^:/]+)[:/](.+)", url):
        return _canonical(m.group(1), m.group(2))

    # HTTP(S): normalise scheme to https, drop any userinfo.
    if m := re.match(r"https?://(?:[^@/]+@)?([^/]+)/(.+)", url):
        return _canonical(m.group(1), m.group(2))

    # Unknown format — return as-is.
    return url


def _canonical(host: str, path: str) -> str:
    host = host.lower()
    if host in _CASE_INSENSITIVE_PATH_HOSTS:
        path = path.lower()
    return f"https://{host}/{path}"


def find_git_config(start: Path) -> Path | None:
    """Walk up from ``start`` until a ``.git/config`` is found."""
    for directory in [start, *start.parents]:
        candidate = directory / ".git" / "config"
        if candidate.exists():
            return candidate
    return None


def remote_uri(start: Path) -> str | None:
    """The canonical repo key of the checkout at ``start``, or ``None``.

    ``origin`` if it is set, else the first remote git lists, normalised.
    Unlike each server's ``repo.detect``, this reads only the path it is given:
    no ``WITAN_REPO``, no process cwd. A language server holds several
    workspace folders in one process, so the folder is the only input that
    says which repo a document belongs to. ``None`` outside a checkout, for a
    checkout with no remote, or when git is not installed.
    """
    url = _git(start, "remote", "get-url", "origin")
    if url is None and (names := _git(start, "remote")):
        url = _git(start, "remote", "get-url", names.splitlines()[0])
    return normalise(url) if url else None


def checkout_branch(start: Path) -> str | None:
    """The raw branch checked out at ``start``, or ``None``.

    ``None`` outside a checkout, on a detached HEAD, or when git is not
    installed. Reads only ``start``, for the reason :func:`remote_uri` gives.
    """
    return _git(start, "symbolic-ref", "--quiet", "--short", "HEAD")


def _git(start: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(start), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    out = result.stdout.strip()
    return out if result.returncode == 0 and out else None
