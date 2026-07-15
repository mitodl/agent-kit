"""Canonical repo-key derivation — the cross-layer join key.

``normalise`` turns any git remote URL into the canonical HTTPS project URI used
as the shared join key across every witan layer (memory, workflow, tasks, and
the code graph) and inside symbol ids (``repo_uri#path::Name``). Both servers
MUST derive it identically or those joins silently break — hence the single
source of truth here, guarded by ``tests/test_repo_key.py``'s golden table.

``detect``/``current_branch`` are intentionally NOT here: they diverge between
the two servers (witan keeps the raw branch; witan-code sanitizes it for
omnigraph-safe branch storage), so each keeps its own in ``repo.py``.
"""

from __future__ import annotations

import re
from pathlib import Path


def normalise(url: str) -> str:
    """Normalise a git remote URL to its canonical HTTPS project URI.

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


def find_git_config(start: Path) -> Path | None:
    """Walk up from ``start`` until a ``.git/config`` is found."""
    for directory in [start, *start.parents]:
        candidate = directory / ".git" / "config"
        if candidate.exists():
            return candidate
    return None
