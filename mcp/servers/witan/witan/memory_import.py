"""Gather the memory coding agents keep on disk, for ``witan memory import``.

Where each agent keeps memory, how to read it and which directory an entry
belongs to is agent-config-kit's (``AgentPlatform.memory``). This module owns
the witan side: which memory kind an entry becomes, and which repo its project
directory is. A directory counts only if it is a git checkout root with a
remote; anything else is reported as unresolved rather than guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from agent_config_kit import MemoryEntry, get_platform
from agent_config_kit.adapters.claude import encode_project
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


#: What ``normalise`` returns for a value it recognised as a remote: anything
#: else it hands back unchanged.
_REPO_URI = re.compile(r"https://[^/\s]+/\S+")


def parse_maps(values: list[str]) -> dict[str, str]:
    """``<project>=<repo>`` pairs, as ``{project: canonical repo URI}``.

    The repo goes through ``normalise``, the canonicaliser every write path's
    explicit ``repo`` goes through, and must come out of it as a repo URI: a
    write path would store anything that does not, and a typo here would file
    every memory of the directory under a repo that does not exist.

    :param values: The ``--map`` arguments.
    :returns: Each project label or path, with the repo it maps to.
    :rtype: dict[str, str]
    :raises ValueError: On a value without ``=``, an empty project, a repo that
        is not a remote URI, or one project given two repos.
    """
    maps: dict[str, str] = {}
    for value in values:
        project, sep, repo = value.rpartition("=")
        project = project.strip()
        if not sep or not project:
            msg = f"--map {value!r} is not <project-dir>=<repo-uri>."
            raise ValueError(msg)
        canonical = normalise(repo)
        if not _REPO_URI.fullmatch(canonical):
            msg = (
                f"--map {value!r}: {repo.strip()!r} is not a repo URI; pass its "
                "remote (e.g. https://github.com/mitodl/agent-kit)."
            )
            raise ValueError(msg)
        if maps.setdefault(project, canonical) != canonical:
            msg = f"--map gives {project} two repos: {maps[project]}, {canonical}."
            raise ValueError(msg)
    return maps


def mapped(project: str, maps: dict[str, str]) -> dict[str, str]:
    """The ``maps`` entries that name ``project``, an entry's project label.

    A key names the project when it is the label itself (what the unresolved
    report prints), or an absolute path whose label it is: the path itself for
    a store that records one (Codex, Copilot), or its encoding for Claude Code,
    whose project directory is named after the path with every character but
    letters and digits turned to ``-``.
    """
    found = {}
    for key, repo in maps.items():
        path = Path(key).expanduser()
        if key == project or (
            path.is_absolute() and project in (str(path), encode_project(str(path)))
        ):
            found[key] = repo
    return found


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
