"""Shared config.toml loading for witan and witan-code.

Both servers read the SAME file (``WITAN_CONFIG`` env var, default
``~/.config/witan/config.toml``) so a single ``[targets.<name>]`` block can
carry overrides for both at once — e.g. witan's ``server``/``graph`` and
witan-code's ``code_dir`` under one name, routed together by the shared
``match_orgs``/``match_repos``/``match_hosts``/``match_paths`` criteria in
``target_config.py``.

Each server keeps its own module-level ``DEFAULT_CONFIG_PATH`` constant
(rather than importing this module's) so its tests can monkeypatch it and
have that module's own ``_load_toml()`` wrapper pick up the change — a
constant imported by value doesn't observe monkeypatching of its origin.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "witan" / "config.toml"


def load_toml(path: Path) -> dict:
    """Load ``WITAN_CONFIG`` path or ``path``. Returns ``{}`` on a missing file.

    Raises ``ValueError`` for a malformed or unreadable file so a
    misconfiguration fails loudly at startup rather than silently falling
    back to defaults.
    """
    resolved = Path(os.environ.get("WITAN_CONFIG", str(path)))
    try:
        with open(resolved, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Failed to parse config file {resolved}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"Failed to read config file {resolved}: {exc}") from exc
