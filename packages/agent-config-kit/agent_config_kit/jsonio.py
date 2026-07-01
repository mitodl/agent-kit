"""JSON config file I/O — JSONC-tolerant load, additive-merge-friendly write.

Moved verbatim from ``witan/setup.py``'s ``_load_json_object``/``_write_json``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


def load_json_object(path: Path) -> dict | None:
    """Return a JSON object from path, or None if it can't be loaded as one.

    A missing file yields an empty dict — a fresh config to populate. A file that
    fails to parse, or parses to a non-object (list/string/number/null), yields
    None so callers skip writing rather than clobbering or crashing on it.
    Handles JSONC (VS Code settings.json allows // comments and trailing commas)
    via a best-effort stripping pass before standard JSON parse.
    """
    if not path.exists():
        return {}
    text = path.read_text()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        stripped = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        # Anchored to line-start (mod leading whitespace) so "//" inside a
        # string value (e.g. a "https://..." URL) isn't mistaken for a
        # comment — only handles whole-line JSONC comments, not trailing
        # end-of-line ones, which is the safer tradeoff.
        stripped = re.sub(r"^\s*//[^\n]*", "", stripped, flags=re.MULTILINE)
        stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def write_json(path: Path, data: dict, dry_run: bool) -> None:
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
