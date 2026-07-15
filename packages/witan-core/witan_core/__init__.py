"""witan-core — shared core for the witan MCP servers.

A leaf package that ``witan`` (witan-council) and ``witan_code`` both depend on,
extracted from the surface they were previously carrying as copy-paste-and-diverge
duplicates. See ``docs/design/witan-core-extraction-spec.md``.

``witan_core`` imports neither ``witan`` nor ``witan_code``: it sits below both,
preserving the one-directional ``witan`` → ``witan_code`` optional-mount DAG.
"""

from __future__ import annotations

from ._detach import popen_detached
from .omnigraph_install import install_omnigraph

__all__ = [
    "install_omnigraph",
    "popen_detached",
]
