"""witan-core — shared core for the witan MCP servers.

A leaf package that ``witan`` (witan-council) and ``witan_code`` both depend on,
extracted from the surface they were previously carrying as copy-paste-and-diverge
duplicates. See ``docs/design/witan-core-extraction-spec.md``.

``witan_core`` imports neither ``witan`` nor ``witan_code``: it sits below both,
preserving the one-directional ``witan`` → ``witan_code`` optional-mount DAG.

The root package is dependency-free (stdlib only). Heavier concerns live in
submodules gated behind extras and are NOT re-exported here: ``witan_core.elicit``
needs ``fastmcp`` (the ``mcp`` extra); the omnigraph installer imports ``rich``
lazily.
"""

from __future__ import annotations

from ._detach import popen_detached
from .config_file import load_toml
from .omnigraph_install import install_omnigraph
from .repo_key import find_git_config, normalise
from .target_config import (
    local_project_path,
    match_target,
    parse_target_tables,
    to_list,
)
from .timeutil import now_iso

__all__ = [
    "find_git_config",
    "install_omnigraph",
    "load_toml",
    "local_project_path",
    "match_target",
    "normalise",
    "now_iso",
    "parse_target_tables",
    "popen_detached",
    "to_list",
]
