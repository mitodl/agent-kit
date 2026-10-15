"""Config-file I/O dispatched on file suffix: ``.toml`` (Codex's
``config.toml``) or JSON for everything else.

TOML goes through ``tomlkit`` rather than ``tomllib`` + a writer because
these files are hand-edited: a ``tomlkit`` document is a ``dict`` the merge
code can mutate in place, and re-serializing it keeps the user's comments,
ordering, and formatting intact.
"""

from __future__ import annotations

from pathlib import Path

import tomlkit
from tomlkit.exceptions import TOMLKitError

from .jsonio import load_json_object, write_json


def _is_toml(path: Path) -> bool:
    return path.suffix == ".toml"


def load_config_object(path: Path) -> dict | None:
    """Same contract as ``load_json_object``: ``{}`` for a missing file,
    ``None`` for one that can't be parsed (so callers skip, not clobber)."""
    if not _is_toml(path):
        return load_json_object(path)
    if not path.exists():
        return tomlkit.document()
    try:
        return tomlkit.parse(path.read_text())
    except TOMLKitError:
        return None


def write_config(path: Path, data: dict, dry_run: bool) -> None:
    if not _is_toml(path):
        write_json(path, data, dry_run)
        return
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tomlkit.dumps(data))
