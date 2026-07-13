"""Global ``--output-format`` state and structured (non-table) rendering.

The CLI's table-producing commands render a rich :class:`~rich.table.Table`
by default (``txt``). ``--output-format json|toml|yaml`` swaps that for a
machine-readable dump of the same rows instead, via :func:`dump_structured`.
"""

from __future__ import annotations

import json
from typing import Literal

import tomli_w
import yaml

OutputFormat = Literal["txt", "json", "toml", "yaml"]

_current_format: OutputFormat = "txt"


def set_output_format(fmt: OutputFormat) -> None:
    global _current_format
    _current_format = fmt


def get_output_format() -> OutputFormat:
    return _current_format


def dump_structured(rows: list[dict[str, str]], title: str, fmt: OutputFormat) -> None:
    """Print ``rows`` (plain, unstyled values) as JSON, TOML, or YAML.

    Wrapped in a ``{title, rows}`` object rather than a bare array — TOML has
    no bare top-level array, so this keeps all three formats consistent.
    Uses plain ``print`` rather than the rich console: these are meant to be
    piped/parsed, and rich's line-wrapping would corrupt the output.
    """
    payload = {"title": title, "rows": rows}
    if fmt == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    elif fmt == "yaml":
        print(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), end="")
    elif fmt == "toml":
        print(tomli_w.dumps(payload), end="")
    else:
        raise ValueError(f"Unsupported output format: {fmt!r}")
