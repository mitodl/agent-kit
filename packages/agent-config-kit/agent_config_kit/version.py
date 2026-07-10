"""Resolve an installed distribution's version for ``--version`` output.

Appends a git short ref when the package is installed editable (``uv pip
install -e``, workspace source) or directly from a VCS URL (``uvx --from
git+...``), since in both cases the PyPI-style version number alone doesn't
pin down which commit is actually running.
"""

from __future__ import annotations

import json
import subprocess
from importlib import metadata
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname


def _git_short_ref(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def resolve_version(dist_name: str) -> str:
    """Return ``<version>`` or ``<version> (<short-ref>)`` for a git/editable install."""
    try:
        dist = metadata.distribution(dist_name)
    except metadata.PackageNotFoundError:
        return "unknown"

    version = dist.version
    direct_url_text = dist.read_text("direct_url.json")
    if not direct_url_text:
        return version

    try:
        direct_url = json.loads(direct_url_text)
    except json.JSONDecodeError:
        return version

    vcs_info = direct_url.get("vcs_info") or {}
    if vcs_info.get("vcs") == "git" and vcs_info.get("commit_id"):
        return f"{version} ({vcs_info['commit_id'][:7]})"

    if direct_url.get("dir_info", {}).get("editable"):
        url = direct_url.get("url")
        if url:
            source_path = Path(url2pathname(urlparse(url).path))
            short_ref = _git_short_ref(source_path)
            if short_ref:
                return f"{version} ({short_ref})"

    return version
