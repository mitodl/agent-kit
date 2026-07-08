"""The user-level global config file (``~/.config/agent-config-kit/config.toml``)
that drives zero-argument ``ac-kit apply`` resolution — spec §7, decision S1.

This module is the config model + loader only. Resolving *which* manifest a
given repo/CWD should use from this config (explicit flags -> repo-local
manifest -> org match -> directory-prefix match -> default) is the zero-arg
``apply`` task's job, not this one.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .fetch import is_remote_uri
from .models import Scope


class ConfigError(Exception):
    """Raised when the global config file fails to parse or validate."""


class OrgConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    manifest: str
    profiles: list[str] = Field(default_factory=list)


class ScopeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match_prefix: str
    manifest: str
    profiles: list[str] = Field(default_factory=list)
    # Named `write_scope`, not `scope` — a bare `scope` field on a `[[scope]]`
    # entry reads as self-referential and collides with manifest
    # [options].scope (PR #76 review). Prefix-routed applies default to
    # writing project-scoped targets in the repo where `apply` runs, not the
    # agent's global location (spec §6.3) — differs from ManifestOptions'
    # own GLOBAL default.
    write_scope: Scope = Scope.PROJECT


class GlobalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_manifest: str | None = None
    default_profiles: list[str] = Field(default_factory=list)
    org: list[OrgConfig] = Field(default_factory=list)
    scope: list[ScopeConfig] = Field(default_factory=list)


def default_config_path() -> Path:
    """``${XDG_CONFIG_HOME:-~/.config}/agent-config-kit/config.toml``. Reads
    the environment and ``Path.home()`` at call time (not cached) so it stays
    testable via ``monkeypatch``, matching ``paths.py``'s convention."""
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config_home) if xdg_config_home else Path.home() / ".config"
    return base / "agent-config-kit" / "config.toml"


def _resolve_config_path(explicit: Path | None) -> Path:
    """``--config`` (``explicit``) beats ``AC_KIT_CONFIG`` beats the XDG default."""
    if explicit is not None:
        return explicit
    if env_path := os.environ.get("AC_KIT_CONFIG"):
        return Path(env_path)
    return default_config_path()


def _expand_path_like(value: str) -> str:
    """``~`` in a local path is expanded; a remote (``https://``/``git+``)
    manifest URI is left untouched."""
    if is_remote_uri(value):
        return value
    return str(Path(value).expanduser())


def _expand_config_paths(data: dict) -> None:
    if (value := data.get("default_manifest")) is not None:
        data["default_manifest"] = _expand_path_like(value)
    for org in data.get("org", []) or []:
        if isinstance(org, dict) and "manifest" in org:
            org["manifest"] = _expand_path_like(org["manifest"])
    for scope in data.get("scope", []) or []:
        if not isinstance(scope, dict):
            continue
        if "match_prefix" in scope:
            scope["match_prefix"] = str(Path(scope["match_prefix"]).expanduser())
        if "manifest" in scope:
            scope["manifest"] = _expand_path_like(scope["manifest"])


def _format_validation_error(exc: ValidationError, path: Path) -> str:
    lines = [f"{path}: config failed validation:"]
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def load_global_config(path: Path | None = None) -> GlobalConfig:
    """Load the global config file. A missing file is a valid, empty config
    (never an error) — zero-arg ``apply`` just falls through to whatever the
    next resolution step in O2's order finds."""
    resolved = _resolve_config_path(path)
    if not resolved.is_file():
        return GlobalConfig()

    try:
        raw_text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{resolved}: could not read config file: {exc}") from exc

    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{resolved}: invalid TOML: {exc}") from exc

    _expand_config_paths(data)

    try:
        return GlobalConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc, resolved)) from exc
