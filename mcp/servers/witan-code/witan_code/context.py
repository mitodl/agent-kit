"""Self-contained UserPromptSubmit status block for witan-code.

Mirrors the shape of ``witan``'s own ``inject-context`` hook, but is a
deliberately independent implementation (no cross-package import — see
``graph.py``'s docstring) so witan-code stays fully usable, and self-announcing,
when installed standalone without ``witan``.

Tells the agent whether the current repo has a code graph ready to query (and
its rough size/freshness) — without this, an agent has no signal that
``code_*`` tools exist or are populated, short of trying one and seeing what
comes back.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import config as cfg_module
from . import repo as repo_module
from .cli import _code_store_stats, _dir_stats

# Matches the lock directory codegraph-session-init.sh creates around a
# background SessionStart index, so this hook can report "indexing in
# progress" instead of a misleadingly empty/stale store. Keyed on the
# sanitized project directory (not a hash) so both the bash hook and this
# module can compute it independently without sharing a hashing scheme.
_LOCK_PREFIX = "codegraph-init-"


def _project_dir() -> Path:
    try:
        cwd = os.getcwd()
    except OSError:  # e.g. the cwd was deleted out from under this process
        cwd = "/"
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", cwd))


def _lock_path(project_dir: Path) -> Path:
    tmp = Path(os.environ.get("TMPDIR", "/tmp"))
    sanitized = str(project_dir).replace("/", "_")
    return tmp / f"{_LOCK_PREFIX}{sanitized}.lock"


def indexing_in_progress() -> bool:
    """Whether codegraph-session-init.sh's background index is still running."""
    return _lock_path(_project_dir()).is_dir()


def inject_context() -> str:
    """A short markdown status block, or "" when there's nothing worth saying.

    Silent when the repo has neither a store nor an index in flight (nothing
    to report), so this hook adds no noise for repos that don't use witan-code.
    """
    cfg = cfg_module.load()
    slug = repo_module.detect()
    if slug is None:
        return ""

    store = cfg_module.store_path(slug, cfg.code_dir)
    in_progress = indexing_in_progress()

    if not store.exists():
        if not in_progress:
            return ""
        return (
            "## Code Graph\n\n"
            f"Indexing `{slug}` for the first time in the background — "
            "`code_*` tools may return partial or empty results until it "
            "finishes.\n"
        )

    repo_uri, file_count = _code_store_stats(store)
    try:
        _, last_indexed = _dir_stats(store)
        freshness = f", last updated {last_indexed}"
    except OSError:  # e.g. a file vanished mid-walk — degrade, don't blank the block
        freshness = ""
    lines = [
        "## Code Graph",
        "",
        f"`{repo_uri}` is indexed: {file_count} files{freshness}.",
    ]
    if in_progress:
        lines.append("A background reindex is currently running.")
    lines.append(
        "Prefer `code_search_symbol` / `code_find_definition` / "
        "`code_find_references` / `code_callers` / `code_impact` over grep "
        "for symbol lookups, call graphs, and change-impact analysis."
    )
    return "\n".join(lines) + "\n"
