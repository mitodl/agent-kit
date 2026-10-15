"""Config-file I/O dispatched on file suffix: ``.toml`` (Codex's
``config.toml``) or JSON for everything else.

TOML goes through ``tomlkit`` rather than ``tomllib`` + a writer because
these files are hand-edited: a ``tomlkit`` document is a ``dict`` the merge
code can mutate in place, and re-serializing it keeps the user's comments,
ordering, and formatting intact.
"""

from __future__ import annotations

import math
import tomllib
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
        path.write_text(_dump_toml(data))


def _dump_toml(doc: tomlkit.TOMLDocument) -> str:
    """tomlkit mis-renders a table added under a top-level dotted key
    (``mcp_servers.x.command = ...``) that has plain keys after it: those keys
    get emitted inside the new table, silently moving e.g. ``model`` into
    ``[mcp_servers.new]``. When the dump doesn't parse back to the same data,
    rewrite the dotted keys as ordinary tables and dump again; if that still
    doesn't round-trip, refuse to write rather than corrupt the file."""
    text = tomlkit.dumps(doc)
    if _same(tomllib.loads(text), doc.unwrap()):
        return text
    dotted = [key.key for key, _ in doc.body if key is not None and key.is_dotted()]
    for name in dotted:
        value = doc[name].unwrap()
        del doc[name]
        doc[name] = value
    text = tomlkit.dumps(doc)
    if not _same(tomllib.loads(text), doc.unwrap()):
        raise ValueError("refusing to write TOML that does not round-trip")
    return text


def _same(a: object, b: object) -> bool:
    """``==`` except that ``nan`` (valid TOML) equals itself."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(map(_same, a, b))
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a):
        return math.isnan(b)
    return a == b
