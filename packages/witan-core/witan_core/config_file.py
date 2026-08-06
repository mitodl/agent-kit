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


def resolve_config_path(path: Path) -> Path:
    """The config file actually in effect: ``WITAN_CONFIG`` if set, else ``path``.

    Split out of :func:`load_toml` so writers (``witan target add``) can target
    the very file the readers read, rather than re-deriving the rule and
    drifting from it.

    An empty or whitespace-only ``WITAN_CONFIG`` counts as unset. Taken
    literally it resolves to ``Path("")`` — the current directory — so a reader
    would report "failed to read config file ." and a writer would try to
    rewrite a directory. That is never what someone means; it is what an
    unexpanded ``WITAN_CONFIG=$SOME_UNSET_VAR`` looks like.
    """
    override = os.environ.get("WITAN_CONFIG", "").strip()
    return Path(override or path).expanduser()


def load_toml(path: Path) -> dict:
    """Load ``WITAN_CONFIG`` path or ``path``. Returns ``{}`` on a missing file.

    Expands ``~`` in the resolved path — ``WITAN_CONFIG`` is commonly set in
    contexts that skip shell tilde-expansion (a Docker/systemd ``Environment=``,
    a CI env block), so a literal ``WITAN_CONFIG=~/.config/witan/config.toml``
    must still resolve rather than fail with a not-found on the literal ``~``.

    Raises ``ValueError`` for a malformed or unreadable file so a
    misconfiguration fails loudly at startup rather than silently falling
    back to defaults.
    """
    resolved = resolve_config_path(path)
    try:
        with open(resolved, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Failed to parse config file {resolved}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"Failed to read config file {resolved}: {exc}") from exc
