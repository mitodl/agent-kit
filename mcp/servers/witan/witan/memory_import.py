"""Gather the memory coding agents keep on disk, for ``witan memory import``.

Where each agent keeps memory, how to read it and which directory an entry
belongs to is agent-config-kit's (``AgentPlatform.memory``). This module owns
the witan side: which memory kind an entry becomes, and which repo its project
directory is. A directory counts only if it is a git checkout root with a
remote; anything else is reported as unresolved rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_config_kit import MemoryEntry, get_platform
from witan_core.repo_key import normalise

from . import repo as repo_module

#: Agent memory type → witan memory kind. ``user`` memories describe a person,
#: so the importer leaves them out unless one is named explicitly.
KIND_BY_TYPE = {
    "project": "project_fact",
    "reference": "project_fact",
    "feedback": "lesson",
    "user": "agent_context",
}


@dataclass(frozen=True)
class Harness:
    """One agent's memory store, as found on this machine.

    ``missing`` says why there is nothing to import from it: the agent keeps
    no memory on disk, or its store is absent or empty.
    """

    name: str
    label: str
    root: Path | None
    entries: list[MemoryEntry]
    missing: str | None = None


@dataclass(frozen=True)
class Resolution:
    """Where an entry's project points: a repo, or why not."""

    repo: str | None
    checkout: Path | None = None
    reason: str | None = None


def kind(entry: MemoryEntry) -> str | None:
    return KIND_BY_TYPE.get(entry.type) if entry.type else None


def harness(name: str) -> Harness:
    """Read the memory store of the agent agent-config-kit calls ``name``."""
    platform = get_platform(name)
    store = platform.memory
    if store is None:
        return Harness(name, platform.name, None, [], "keeps no memory on disk")
    if not store.root.exists():
        return Harness(
            name, platform.name, store.root, [], f"no memory store at {store.root}"
        )
    entries = store.entries()
    missing = None if entries else f"no memories in {store.root}"
    return Harness(name, platform.name, store.root, entries, missing)


def resolve(entry: MemoryEntry) -> Resolution:
    """Map an entry's project directory to a canonical repo URI.

    Only a candidate that is itself a checkout root (has ``.git``) counts, so a
    directory that merely sits inside some other repository (a home directory
    under dotfile management, a folder of checkouts) does not borrow that
    repository's remote. Two candidates on one repo (``x-y`` and ``x.y``
    clones of one remote, which Claude Code names alike) resolve; two repos
    are ambiguous.
    """
    if not entry.candidates:
        return Resolution(None, reason="no directory on disk matches this project")
    found: dict[str, Path] = {}
    for path in entry.candidates:
        if not (path / ".git").exists():
            continue
        if url := repo_module.git_remote_url(path):
            found.setdefault(normalise(url), path)
    if not found:
        paths = ", ".join(str(p) for p in entry.candidates)
        return Resolution(None, reason=f"not a git checkout with a remote: {paths}")
    if len(found) > 1:
        detail = ", ".join(f"{p} ({r})" for r, p in found.items())
        return Resolution(None, reason=f"ambiguous: {detail}")
    (repo, path), *_ = found.items()
    return Resolution(repo, checkout=path)
