"""Global ``--output-format`` state and structured rendering.

The CLI's table-producing commands render a rich :class:`~rich.table.Table`
by default (``txt``). ``--output-format json|toml|yaml`` swaps that for a
machine-readable ``{title, rows}`` dump of the records behind the table (not
its display cells), via :func:`dump_record`. An empty table dumps
``rows: []``, never the prose the ``txt`` view prints. Commands whose output
is one record print it through :func:`dump_record` directly.
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


def _drop_nulls(value: object) -> object:
    if isinstance(value, dict):
        return {k: _drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_nulls(v) for v in value if v is not None]
    return value


def dump_record(payload: dict[str, object], fmt: OutputFormat) -> None:
    """Print one record as JSON, TOML, or YAML, as the tool returned it.

    ``task <slug>`` and ``project status`` print a tool's record here; the
    list commands print a ``{title, rows}`` envelope of the records behind each
    list, most
    through :func:`witan.cli._common.render_table`. Wrapped rather than a bare
    array because TOML has no top-level array. Plain ``print``, not the rich
    console, because rich's line-wrapping would corrupt a piped document.
    JSON and YAML keep ``null``. TOML has none, so a ``None`` there is omitted
    rather than rewritten to ``""``, which would make an unset ``closed_at``
    read as present.
    """
    if fmt == "toml":
        payload = _drop_nulls(payload)
    if fmt == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    elif fmt == "yaml":
        print(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), end="")
    elif fmt == "toml":
        print(tomli_w.dumps(payload), end="")
    else:
        raise ValueError(f"Unsupported output format: {fmt!r}")
