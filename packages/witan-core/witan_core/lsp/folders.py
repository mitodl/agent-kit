"""Resolve a workspace folder to its repo, branch and ``[targets.<name>]`` block.

The CLIs route by the process cwd (or ``CLAUDE_PROJECT_DIR``). A language
server serves several folders from one process, so it routes each one with the
folder as the path, through the same ``match_*`` rules
(:func:`witan_core.target_config.match_target_at`). ``--target`` and
``WITAN_TARGET`` still win over matching, as on the CLI.

stdlib only, like the rest of the shared target routing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..config_file import DEFAULT_CONFIG_PATH, load_toml
from ..repo_key import checkout_branch, remote_uri
from ..target_config import (
    match_target_at,
    named_target,
    parse_target_tables,
    to_list,
)
from .plugin import Folder

__all__ = ["explicit_target", "resolve_folder"]


@dataclass(frozen=True)
class _TargetMatch:
    name: str
    match_orgs: list[str] = field(default_factory=list)
    match_repos: list[str] = field(default_factory=list)
    match_hosts: list[str] = field(default_factory=list)
    match_paths: list[str] = field(default_factory=list)


def _targets() -> list[_TargetMatch]:
    return [
        _TargetMatch(
            name=name,
            match_orgs=to_list(cfg.get("match_orgs")),
            match_repos=to_list(cfg.get("match_repos")),
            match_hosts=to_list(cfg.get("match_hosts")),
            match_paths=to_list(cfg.get("match_paths")),
        )
        for name, cfg in parse_target_tables(load_toml(DEFAULT_CONFIG_PATH)).items()
    ]


def explicit_target(flag: str | None) -> str | None:
    """The target named by ``--target`` or ``WITAN_TARGET``, checked to exist.

    Raises ``ValueError`` for a name config.toml does not define
    (:func:`witan_core.target_config.named_target`): routing every folder by matching instead would send
    reads somewhere the person did not ask for.
    """
    name = flag or os.environ.get("WITAN_TARGET")
    if not name:
        return None
    return named_target(_targets(), name).name


def resolve_folder(uri: str, path: Path, explicit: str | None) -> Folder:
    """Resolve one workspace folder. Reads config.toml each time, so a target
    added while the editor is open applies to folders opened after it."""
    if explicit is None:
        selected = match_target_at(_targets(), path)
        target = selected.name if selected else None
    else:
        target = explicit
    return Folder(
        uri=uri,
        path=path,
        repo=remote_uri(path),
        branch=checkout_branch(path),
        target=target,
    )
