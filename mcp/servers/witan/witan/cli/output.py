"""Global ``--output-format`` state and structured (non-table) rendering.

The CLI's table-producing commands render a rich :class:`~rich.table.Table`
by default (``txt``). ``--output-format json|toml|yaml`` swaps that for a
machine-readable dump of the same rows instead, via :func:`dump_structured`.
An empty table dumps ``rows: []``, never the prose the ``txt`` view prints.
Commands whose output is a record rather than a table go through
:func:`dump_record`.
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


def dump_structured(
    rows: list[dict[str, object]], title: str, fmt: OutputFormat
) -> None:
    """Print ``rows`` as JSON, TOML, or YAML, preserving each value's native type.

    Wrapped in a ``{title, rows}`` object rather than a bare array — TOML has
    no bare top-level array, so this keeps all three formats consistent.
    Uses plain ``print`` rather than the rich console: these are meant to be
    piped/parsed, and rich's line-wrapping would corrupt the output. Callers
    go through :func:`witan.cli._common.render_table`, which normalizes
    ``None`` to ``""`` first — TOML has no null.
    """
    dump_record({"title": title, "rows": rows}, fmt)


def _drop_nulls(value: object) -> object:
    if isinstance(value, dict):
        return {k: _drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_nulls(v) for v in value if v is not None]
    return value


def dump_record(payload: dict[str, object], fmt: OutputFormat) -> None:
    """Print one record as JSON, TOML, or YAML, as the tool returned it.

    For output that is not a table: ``task <slug>`` and ``project status``
    print a tool's record, ``project tasks`` and ``session list`` its rows.
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
