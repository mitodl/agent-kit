"""Structured-output helpers for the ``witan-code`` CLI."""

from __future__ import annotations

import json
from typing import Literal

import tomli_w
import yaml

OutputFormat = Literal["txt", "json", "toml", "yaml"]

_output_format: OutputFormat = "txt"


def set_output_format(fmt: OutputFormat) -> None:
    global _output_format
    _output_format = fmt


def get_output_format() -> OutputFormat:
    return _output_format


def dump_structured(
    rows: list[dict[str, object]], title: str, fmt: OutputFormat
) -> None:
    """Print ``rows`` as JSON, TOML, or YAML.

    The wrapper object keeps TOML consistent with the other formats because TOML
    has no bare top-level array. Callers normalize ``None`` before reaching this
    helper so TOML never receives unsupported null values.
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
