"""witan-core — shared core for the witan MCP servers.

A leaf package that ``witan`` (witan-council) and ``witan_code`` both depend on,
extracted from the surface they were previously carrying as copy-paste-and-diverge
duplicates. See ``docs/design/witan-core-extraction-spec.md``.

``witan_core`` imports neither ``witan`` nor ``witan_code``: it sits below both,
preserving the one-directional ``witan`` → ``witan_code`` optional-mount DAG.

Nothing is extracted into this package yet — the incremental extraction tasks
land modules here and delete the duplicated copies from each server.
"""

from __future__ import annotations

__all__: list[str] = []
