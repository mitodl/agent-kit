"""The user-level global config file (``~/.config/agent-config-kit/config.toml``)
that drives zero-argument ``agent-kit apply`` resolution — spec §7, decision S1.

This module is the config model + loader only. Resolving *which* manifest a
given repo/CWD should use from this config (explicit flags -> repo-local
manifest -> org match -> directory-prefix match -> default) is the zero-arg
``apply`` task's job, not this one.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .fetch import is_remote_uri
from .models import Scope


class ConfigError(Exception):
    """Raised when the global config file fails to parse or validate."""


# `overlay`'s value shape mirrors `ManifestBundle`'s own tables
# (`mcp_servers`/`skills`/`hooks`/`lsp_servers`) verbatim — see
# `agent-config-kit-config-overlay-spec.md` I2. Kept as a permissive raw
# dict here, not a typed model, for the same reason `ManifestBundle.skills`
# is (`manifest.py`): real per-entry validation happens once, downstream,
# via `manifest.load_overlay_bundle`, so a malformed entry raises the same
# clean `ManifestError` a malformed manifest entry would rather than a
# pydantic union error naming the wrong field.


class OrgConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    manifest: str
    profiles: list[str] = Field(default_factory=list)
    overlay: dict[str, Any] = Field(default_factory=dict)


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
    overlay: dict[str, Any] = Field(default_factory=dict)


class GlobalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_manifest: str | None = None
    default_profiles: list[str] = Field(default_factory=list)
    org: list[OrgConfig] = Field(default_factory=list)
    scope: list[ScopeConfig] = Field(default_factory=list)
    overlay: dict[str, Any] = Field(default_factory=dict)


def default_config_path() -> Path:
    """``${XDG_CONFIG_HOME:-~/.config}/agent-config-kit/config.toml``. Reads
    the environment and ``Path.home()`` at call time (not cached) so it stays
    testable via ``monkeypatch``, matching ``paths.py``'s convention."""
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config_home) if xdg_config_home else Path.home() / ".config"
    return base / "agent-config-kit" / "config.toml"


def resolve_config_path(explicit: Path | None) -> Path:
    """``--config`` (``explicit``) beats ``AC_KIT_CONFIG`` beats the XDG
    default. Public — the CLI's ``config init`` command (cli.py) resolves the
    same path this way so it writes exactly where ``load_global_config``
    would read from."""
    if explicit is not None:
        return explicit
    if env_path := os.environ.get("AC_KIT_CONFIG"):
        return Path(env_path)
    return default_config_path()


def _expand_path_like(value: object) -> object:
    """``~`` in a local path string is expanded; a remote (``https://``/
    ``git+``) manifest URI is left untouched. A non-string value (e.g. a
    manifest author's typo like ``default_manifest = 123``) is returned
    unchanged rather than raising here — ``GlobalConfig.model_validate``
    reports the type mismatch as a clean ``ConfigError`` afterward instead
    of this function crashing with a raw ``TypeError`` first."""
    if not isinstance(value, str):
        return value
    if is_remote_uri(value):
        return value
    return str(Path(value).expanduser())


def _expand_overlay_paths(overlay: object) -> None:
    """`~` in an overlay's `skill_md_path`/`entry_path` values is expanded
    here, same as `default_manifest`/`[[org]].manifest`/`[[scope]].manifest`
    already are — an overlay entry lives directly in `config.toml`, so it
    gets the same `~` support the rest of that file has. A manifest's own
    `skill_md_path`/`entry_path` values don't get this treatment (M5:
    relative-to-manifest-dir or absolute only) — this only applies to
    overlay tables, which are config.toml's own content, not a manifest's."""
    if not isinstance(overlay, dict):
        return
    skills = overlay.get("skills")
    if isinstance(skills, dict):
        for name, value in skills.items():
            if isinstance(value, str):
                skills[name] = _expand_path_like(value)
            elif isinstance(value, dict) and "skill_md_path" in value:
                value["skill_md_path"] = _expand_path_like(value["skill_md_path"])
    hooks = overlay.get("hooks")
    # `overlay` is intentionally left as an unvalidated raw dict here (real
    # shape validation happens downstream, in
    # manifest.load_overlay_bundle, so a malformed entry raises the same
    # clean ManifestError a malformed manifest entry would) — so `hooks`
    # could be anything, e.g. `hooks = 1`. Only walk it here if it's
    # actually a list; a wrong shape is that downstream validator's job to
    # report, not a raw TypeError from iterating a non-iterable here.
    if isinstance(hooks, list):
        for hook in hooks:
            if (
                isinstance(hook, dict)
                and hook.get("kind") == "plugin"
                and "entry_path" in hook
            ):
                hook["entry_path"] = _expand_path_like(hook["entry_path"])


def _expand_config_paths(data: dict) -> None:
    if (value := data.get("default_manifest")) is not None:
        data["default_manifest"] = _expand_path_like(value)
    _expand_overlay_paths(data.get("overlay"))
    for org in data.get("org", []) or []:
        if not isinstance(org, dict):
            continue
        if "manifest" in org:
            org["manifest"] = _expand_path_like(org["manifest"])
        _expand_overlay_paths(org.get("overlay"))
    for scope in data.get("scope", []) or []:
        if not isinstance(scope, dict):
            continue
        if "match_prefix" in scope:
            scope["match_prefix"] = _expand_path_like(scope["match_prefix"])
        if "manifest" in scope:
            scope["manifest"] = _expand_path_like(scope["manifest"])
        _expand_overlay_paths(scope.get("overlay"))


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
    resolved = resolve_config_path(path)
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
