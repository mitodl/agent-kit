"""TOML manifest loading — a declarative alternative to building a
``RegistrationBundle`` in Python by hand.

Part of the base package (only ``tomllib`` + existing ``models``/``plan``/
``registry`` — no ``cli``-extra dependency) so a programmatic caller can load
a manifest without pulling in ``cyclopts``/``rich``. See
``docs/design/agent-config-kit-cli-spec.md`` §3 for the full format spec.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .fetch import FetchError, fetch_remote, is_remote_uri
from .models import Hook, LspServer, McpServer, Scope, SkillSource
from .plan import RegistrationBundle
from .prune import hook_identity
from .registry import known_platforms


class ManifestError(Exception):
    """Raised when a manifest file fails to parse or validate."""


class ManifestBundle(BaseModel):
    """Validate-on-load mirror of ``RegistrationBundle``'s shape.

    ``skills`` stays a permissive ``dict[str, Any]`` here (name -> a string
    ``skill_md_path`` shorthand, or an inline table) rather than
    ``dict[str, SkillSource]`` — ``_build_skill_sources`` does the real
    per-entry validation afterward so it can raise a ``ManifestError`` naming
    the offending skill key instead of a generic pydantic union error.
    """

    model_config = ConfigDict(extra="forbid")

    instructions: str | None = None
    mcp_servers: dict[str, McpServer] = Field(default_factory=dict)
    lsp_servers: dict[str, LspServer] = Field(default_factory=dict)
    hooks: list[Hook] = Field(default_factory=list)
    skills: dict[str, Any] = Field(default_factory=dict)


class _ManifestOptionsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Scope = Scope.GLOBAL
    platforms: list[str] | None = None
    default_profiles: list[str] = Field(default_factory=list)


@dataclass
class ManifestOptions:
    scope: Scope = Scope.GLOBAL
    platforms: list[str] | None = None  # None => apply_all's own detection
    default_profiles: list[str] = field(default_factory=list)


class _ProfileModel(BaseModel):
    """A ``[profiles.<name>]`` table (spec §4.1) — each field is a list of
    entry keys selected from the manifest's own top-level tables, plus
    ``inherits`` to union with other profiles first."""

    model_config = ConfigDict(extra="forbid")

    inherits: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    hooks: list[str] = Field(default_factory=list)
    lsp_servers: list[str] = Field(default_factory=list)


@dataclass
class ProfileDef:
    inherits: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)
    lsp_servers: list[str] = field(default_factory=list)


@dataclass
class Manifest:
    bundle: RegistrationBundle
    options: ManifestOptions
    path: Path  # the manifest file this was loaded from
    profiles: dict[str, ProfileDef] = field(default_factory=dict)


def _resolve_path_field(
    value: object,
    manifest_dir: Path,
    *,
    manifest_path: Path,
    field: str,
    cache_dir: Path,
) -> str:
    """Resolve a manifest-relative path against the manifest's own
    directory, not the process CWD (spec M5). Absolute paths pass through.
    A remote (``https://``/``git+``) URI is fetched into ``cache_dir`` and
    replaced with the resulting local path — see ``fetch.py``."""
    if not isinstance(value, str):
        raise ManifestError(
            f"{manifest_path}: {field} must be a string path, got "
            f"{type(value).__name__}: {value!r}"
        )
    if is_remote_uri(value):
        try:
            return str(fetch_remote(value, cache_dir))
        except FetchError as exc:
            raise ManifestError(f"{manifest_path}: {field}: {exc}") from exc
    candidate = Path(value)
    return str(candidate) if candidate.is_absolute() else str(manifest_dir / candidate)


def _resolve_relative_paths(
    data: dict, manifest_dir: Path, manifest_path: Path, cache_dir: Path
) -> None:
    for hook in data.get("hooks", []) or []:
        if (
            isinstance(hook, dict)
            and hook.get("kind") == "plugin"
            and "entry_path" in hook
        ):
            hook["entry_path"] = _resolve_path_field(
                hook["entry_path"],
                manifest_dir,
                manifest_path=manifest_path,
                field="hooks[].entry_path",
                cache_dir=cache_dir,
            )

    skills_data = data.get("skills", {}) or {}
    if not isinstance(skills_data, dict):
        raise ManifestError(
            f"{manifest_path}: [skills] must be a table of skill-name -> "
            f"path/table (the old [[skills]] array form is no longer "
            f"supported), got {type(skills_data).__name__}"
        )
    for name, value in skills_data.items():
        if isinstance(value, str):
            skills_data[name] = _resolve_path_field(
                value,
                manifest_dir,
                manifest_path=manifest_path,
                field=f"skills.{name}",
                cache_dir=cache_dir,
            )
        elif isinstance(value, dict) and "skill_md_path" in value:
            value["skill_md_path"] = _resolve_path_field(
                value["skill_md_path"],
                manifest_dir,
                manifest_path=manifest_path,
                field=f"skills.{name}.skill_md_path",
                cache_dir=cache_dir,
            )


def _build_skill_sources(
    skills: dict[str, Any], manifest_path: Path
) -> list[SkillSource]:
    """Normalize the manifest's ``[skills]`` name-keyed table (F1: each value
    is either a string ``skill_md_path`` shorthand or an inline table) into
    ``RegistrationBundle``'s ``list[SkillSource]`` shape."""
    sources = []
    for name, value in skills.items():
        if isinstance(value, str):
            skill_md_path = value
        elif isinstance(value, dict):
            if "skill_md_path" not in value:
                raise ManifestError(
                    f"{manifest_path}: skills.{name}: table form requires a "
                    "'skill_md_path' key"
                )
            skill_md_path = value["skill_md_path"]
        else:
            raise ManifestError(
                f"{manifest_path}: skills.{name}: must be a string path or an "
                f"inline table, got {type(value).__name__}: {value!r}"
            )
        try:
            sources.append(SkillSource(name=name, skill_md_path=Path(skill_md_path)))
        except ValidationError as exc:
            raise ManifestError(_format_validation_error(exc, manifest_path)) from exc
    return sources


def _parse_profiles(data: dict, path: Path) -> dict[str, ProfileDef]:
    profiles_data = data.get("profiles", {}) or {}
    if not isinstance(profiles_data, dict):
        raise ManifestError(
            f"{path}: [profiles] must be a table of profile-name -> table, got "
            f"{type(profiles_data).__name__}"
        )
    profiles: dict[str, ProfileDef] = {}
    for name, prof_data in profiles_data.items():
        try:
            prof_model = _ProfileModel.model_validate(prof_data)
        except ValidationError as exc:
            raise ManifestError(_format_validation_error(exc, path)) from exc
        profiles[name] = ProfileDef(
            inherits=prof_model.inherits,
            skills=prof_model.skills,
            mcp_servers=prof_model.mcp_servers,
            hooks=prof_model.hooks,
            lsp_servers=prof_model.lsp_servers,
        )
    return profiles


def _detect_profile_cycles(profiles: dict[str, ProfileDef], path: Path) -> None:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(profiles, WHITE)

    def _visit(name: str, chain: list[str]) -> None:
        color[name] = GRAY
        for parent in profiles[name].inherits:
            if parent not in profiles:
                raise ManifestError(
                    f"{path}: profiles.{name}.inherits: no such profile {parent!r}"
                )
            if color[parent] == GRAY:
                cycle = " -> ".join([*chain, name, parent])
                raise ManifestError(f"{path}: profile inheritance cycle: {cycle}")
            if color[parent] == WHITE:
                _visit(parent, [*chain, name])
        color[name] = BLACK

    for name in profiles:
        if color[name] == WHITE:
            _visit(name, [])


def _validate_profile_entry_refs(
    profiles: dict[str, ProfileDef], bundle: RegistrationBundle, path: Path
) -> None:
    """A profile's entry-key lists must reference real top-level entries —
    fail fast on a typo'd reference (spec §4.2 step 3), at load time so this
    is caught even for a profile that isn't selected this run."""
    valid_keys = {
        "skills": {s.name for s in bundle.skills},
        "mcp_servers": set(bundle.mcp_servers),
        "hooks": {hook_identity(h) for h in bundle.hooks},
        "lsp_servers": set(bundle.lsp_servers),
    }
    for name, profile in profiles.items():
        for field_name, keys in valid_keys.items():
            for key in getattr(profile, field_name):
                if key not in keys:
                    raise ManifestError(
                        f"{path}: profiles.{name}.{field_name}: {key!r} does "
                        "not match any top-level entry"
                    )


def resolve_profile(manifest: Manifest, names: list[str]) -> RegistrationBundle:
    """Union the resolved entries of every named profile, expanding
    ``inherits`` transitively (spec §4.2). ``names=[]`` (no profile selected)
    returns the manifest's full bundle unchanged — profiles are opt-in
    filters, not gates (O-DEFAULT); a manifest can still narrow the
    zero-selection default via ``[options] default_profiles``."""
    if not names:
        return manifest.bundle

    selected: dict[str, set[str]] = {
        "skills": set(),
        "mcp_servers": set(),
        "hooks": set(),
        "lsp_servers": set(),
    }
    seen: set[str] = set()

    def _collect(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        profile = manifest.profiles.get(name)
        if profile is None:
            raise ManifestError(
                f"{manifest.path}: --profile {name!r}: no such profile in this manifest"
            )
        for parent in profile.inherits:
            _collect(parent)
        selected["skills"].update(profile.skills)
        selected["mcp_servers"].update(profile.mcp_servers)
        selected["hooks"].update(profile.hooks)
        selected["lsp_servers"].update(profile.lsp_servers)

    for name in names:
        _collect(name)

    # Filter the manifest's own ordered lists rather than iterating the
    # `selected` sets directly — set iteration order is randomized per
    # process (PYTHONHASHSEED), which would make the resolved bundle's
    # hook/skill order nondeterministic across runs (PR #78 review).
    return RegistrationBundle(
        mcp_servers={
            k: v
            for k, v in manifest.bundle.mcp_servers.items()
            if k in selected["mcp_servers"]
        },
        hooks=[
            h for h in manifest.bundle.hooks if hook_identity(h) in selected["hooks"]
        ],
        skills=[s for s in manifest.bundle.skills if s.name in selected["skills"]],
        lsp_servers={
            k: v
            for k, v in manifest.bundle.lsp_servers.items()
            if k in selected["lsp_servers"]
        },
        # Profile-independent scalar in v1 (O-INSTR, spec §9).
        instructions=manifest.bundle.instructions,
    )


def _format_validation_error(exc: ValidationError, path: Path) -> str:
    lines = [f"{path}: manifest failed validation:"]
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def default_cache_dir(manifest_path: Path) -> Path:
    """Where remote (``https://``/``git+``) skill/hook sources fetched by
    this manifest are cached, by default — alongside the manifest itself,
    matching M5's "resolved relative to the manifest's own directory"
    convention for local paths."""
    return manifest_path.parent / ".agent-config-kit-cache"


def load_manifest(path: Path, *, cache_dir: Path | None = None) -> Manifest:
    path = Path(path)

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"{path}: could not read manifest file: {exc}") from exc

    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{path}: invalid TOML: {exc}") from exc

    manifest_dir = path.parent
    resolved_cache_dir = cache_dir if cache_dir is not None else default_cache_dir(path)
    _resolve_relative_paths(data, manifest_dir, path, resolved_cache_dir)

    options_data = data.pop("options", {})
    try:
        options_model = _ManifestOptionsModel.model_validate(options_data)
    except ValidationError as exc:
        raise ManifestError(_format_validation_error(exc, path)) from exc

    if options_model.platforms is not None:
        unknown = sorted(set(options_model.platforms) - set(known_platforms()))
        if unknown:
            raise ManifestError(
                f"{path}: [options] unknown platform(s): {', '.join(unknown)} "
                f"(known: {', '.join(known_platforms())})"
            )

    profiles = _parse_profiles(data, path)
    data.pop("profiles", None)

    try:
        bundle_model = ManifestBundle.model_validate(data)
    except ValidationError as exc:
        raise ManifestError(_format_validation_error(exc, path)) from exc

    bundle = RegistrationBundle(
        mcp_servers=bundle_model.mcp_servers,
        hooks=bundle_model.hooks,
        skills=_build_skill_sources(bundle_model.skills, path),
        lsp_servers=bundle_model.lsp_servers,
        instructions=bundle_model.instructions,
    )

    _detect_profile_cycles(profiles, path)
    _validate_profile_entry_refs(profiles, bundle, path)

    if options_model.default_profiles:
        unknown_profiles = sorted(set(options_model.default_profiles) - set(profiles))
        if unknown_profiles:
            raise ManifestError(
                f"{path}: [options] default_profiles: no such profile(s): "
                f"{', '.join(unknown_profiles)}"
            )

    options = ManifestOptions(
        scope=options_model.scope,
        platforms=options_model.platforms,
        default_profiles=options_model.default_profiles,
    )
    return Manifest(bundle=bundle, options=options, path=path, profiles=profiles)
