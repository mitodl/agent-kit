"""Prune/uninstall support for ``agent-kit apply --prune``.

``apply()``/``apply_all()`` are pure additive-merge (``plan.py``'s module
docstring) — nothing removes a previously written entry when it drops out of
a manifest. This module tracks what a manifest last wrote in a *state file*
and, on a later ``apply --prune``, removes exactly the entries that were
written before but are no longer in the manifest — never touching a key the
manifest never owned. See ``docs/design/agent-config-kit-cli-spec.md`` §5 for
the full design.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import registry
from .installers import skill_files
from .jsonio import load_json_object, write_json
from .models import (
    SKILL_NAME_PATTERN,
    DeclarativeHook,
    Hook,
    HookEvent,
    PluginRegistration,
    Scope,
)
from .plan import InstallResult, RegistrationBundle, _navigate, _resolve_target, apply


def hook_identity(hook: Hook) -> str:
    """Stable-across-runs identity for a hook — a hook that changes its own
    fields is, for prune purposes, a different hook (matches the granularity
    ``apply()`` already uses implicitly for merge/dedup)."""
    if isinstance(hook, DeclarativeHook):
        return f"declarative:{hook.event.value}:{hook.command}"
    assert isinstance(hook, PluginRegistration)
    return f"plugin:{hook.entry_path.name}"


def _decode_hook_identity(identity: str) -> Hook:
    kind, rest = identity.split(":", 1)
    if kind == "declarative":
        event, command = rest.split(":", 1)
        return DeclarativeHook(event=HookEvent(event), command=command)
    return PluginRegistration(entry_path=Path(rest))


@dataclass
class PlatformState:
    mcp_servers: list[str] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)
    skills: list[str] = field(
        default_factory=list
    )  # "<skill-name>/<relative-file-path>", not just skill names — see
    # _bundle_platform_state


def _bundle_platform_state(bundle: RegistrationBundle) -> PlatformState:
    # Unlike mcp_servers/hooks, a skill's supporting files (scripts/,
    # references/, assets/, ...) are never enumerated in the manifest — only
    # its name and SKILL.md path are. Tracking skills by name alone would mean
    # a skill that drops a file (but keeps its name) between two applies
    # could never have that stale file pruned, since name-level diffing sees
    # no change at all. Tracking one entry per actual file — re-derived from
    # disk via skill_files, the same walk install_skills uses to copy — makes
    # prune diff at the same granularity apply() actually writes at.
    skill_entries = [
        f"{skill.name}/{rel.as_posix()}"
        for skill in bundle.skills
        for rel in skill_files(skill)
    ]
    return PlatformState(
        mcp_servers=sorted(bundle.mcp_servers),
        hooks=sorted(hook_identity(h) for h in bundle.hooks),
        skills=sorted(skill_entries),
    )


def manifest_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def default_state_path(manifest_path: Path) -> Path:
    return manifest_path.with_name(manifest_path.name + ".lock.json")


def load_state(path: Path) -> dict[str, PlatformState]:
    """Return ``{platform_name: PlatformState}`` describing what a prior
    ``apply --prune`` wrote for each platform. A missing file means no prior
    state for any platform — the caller's first-ever prune run must then
    prune nothing (there is nothing safe to diff against)."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return {
        name: PlatformState(
            mcp_servers=list(platform_data.get("mcp_servers", [])),
            hooks=list(platform_data.get("hooks", [])),
            skills=list(platform_data.get("skills", [])),
        )
        for name, platform_data in data.get("platforms", {}).items()
    }


def write_state(
    path: Path, manifest_path: Path, platforms: dict[str, PlatformState]
) -> None:
    """Write the full per-platform state back. Callers must have merged
    freshly-applied platforms into a dict already loaded from ``path`` (via
    ``load_state``) rather than starting from ``{}`` — otherwise a
    single-platform run (e.g. ``--platform claude``) would erase every other
    platform's previously recorded state (spec §5 step 5).

    Writes via a temp file + atomic rename so a crash or interrupt mid-write
    can never leave a truncated/corrupt state file behind — this file is the
    only record of what prune is allowed to remove later, so a half-written
    one would be actively unsafe, not just inconvenient.
    """
    data = {
        "manifest_hash": manifest_hash(manifest_path),
        "platforms": {
            name: {
                "mcp_servers": state.mcp_servers,
                "hooks": state.hooks,
                "skills": state.skills,
            }
            for name, state in platforms.items()
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2) + "\n")
    tmp_path.replace(path)


def _remove_mcp_servers(
    platform: registry.AgentPlatform, names: set[str], *, scope: Scope, dry_run: bool
) -> list[str]:
    if not names or platform.mcp is None:
        return []
    target = _resolve_target(platform.mcp, scope)
    if target is None:
        return []
    cfg = load_json_object(target.path)
    if cfg is None:
        return []
    container = _navigate(cfg, target.key_path)
    removed = [name for name in sorted(names) if name in container]
    for name in removed:
        del container[name]
    if removed:
        write_json(target.path, cfg, dry_run)
    return removed


def _remove_hooks(
    platform: registry.AgentPlatform,
    identities: set[str],
    *,
    scope: Scope,
    dry_run: bool,
) -> list[str]:
    if not identities or platform.hooks is None:
        return []
    decoded = [
        (identity, _decode_hook_identity(identity)) for identity in sorted(identities)
    ]
    declarative = [(i, h) for i, h in decoded if isinstance(h, DeclarativeHook)]
    plugins = [(i, h) for i, h in decoded if isinstance(h, PluginRegistration)]
    removed: list[str] = []

    if declarative and platform.hooks_remove is not None:
        target = _resolve_target(platform.hooks, scope)
        if target is not None:
            cfg = load_json_object(target.path)
            if cfg is not None:
                changed = False
                for identity, hook in declarative:
                    if platform.hooks_remove(cfg, [hook]):
                        removed.append(identity)
                        changed = True
                if changed:
                    write_json(target.path, cfg, dry_run)

    if plugins and platform.hooks_merge is None:
        target = _resolve_target(platform.hooks, scope)
        if target is not None:
            for identity, hook in plugins:
                dest = target.path / hook.entry_path.name
                if dest.is_file():
                    if not dry_run:
                        dest.unlink()
                    removed.append(identity)

    return removed


def _prune_empty_dirs(start: Path, *, stop_above: Path) -> None:
    """Remove ``start`` and any now-empty ancestor directories, stopping
    before (never removing) ``stop_above`` itself — e.g. a platform's shared
    ``skills/`` dir must survive even after every skill under it is gone."""
    current = start
    while current != stop_above and current.is_dir() and not any(current.iterdir()):
        current.rmdir()
        current = current.parent


def _is_safe_relative_path(rel: str) -> bool:
    """Reject anything that could escape ``dest_base / skill_name`` once
    joined — an absolute path, or any ``..`` segment."""
    if not rel:
        return False
    path = Path(rel)
    return not path.is_absolute() and ".." not in path.parts


def _remove_skills(
    platform: registry.AgentPlatform, entries: set[str], *, scope: Scope, dry_run: bool
) -> list[Path]:
    """``entries`` are ``"<skill-name>/<relative-file-path>"`` strings (see
    ``_bundle_platform_state``) — removed at file granularity so a skill that
    only dropped one supporting file (not its whole self) gets exactly that
    file pruned, with now-empty directories (e.g. an emptied ``scripts/``, or
    the skill's own dir once every file is gone) cleaned up behind it.

    Both halves of ``entry`` come straight from the state file's JSON — an
    externally-editable file, not something that goes through
    ``SkillSource``'s own validation — so each is re-checked here before
    being used to build a filesystem path. An entry that fails either check
    is skipped rather than raising: it can only mean the state file was
    hand-edited or corrupted, and the safe response to "I can't fully trust
    this entry" is to leave it alone, not to guess.
    """
    if not entries or platform.skills is None:
        return []
    target = _resolve_target(platform.skills, scope)
    if target is None:
        return []
    dest_dirs = (
        platform.skill_dest_dirs(target.path)
        if platform.skill_dest_dirs
        else [target.path]
    )
    removed: list[Path] = []
    for entry in sorted(entries):
        skill_name, _, rel = entry.partition("/")
        if not SKILL_NAME_PATTERN.fullmatch(skill_name) or not _is_safe_relative_path(
            rel
        ):
            continue
        for dest_base in dest_dirs:
            dest_file = dest_base / skill_name / rel
            if dest_file.is_file():
                if not dry_run:
                    dest_file.unlink()
                    _prune_empty_dirs(dest_file.parent, stop_above=dest_base)
                removed.append(dest_file)
    return removed


def apply_with_prune(
    platform_name: str,
    bundle: RegistrationBundle,
    previous: PlatformState,
    *,
    scope: Scope = Scope.GLOBAL,
    dry_run: bool = False,
) -> tuple[InstallResult, PlatformState]:
    """Apply the manifest, then remove exactly the entries ``previous``
    recorded that are no longer in ``bundle``. Returns the usual
    ``InstallResult`` (with ``removed`` populated) plus the ``PlatformState``
    to persist for this platform going forward."""
    result = apply(platform_name, bundle, scope=scope, dry_run=dry_run)
    platform = registry.get_platform(platform_name)
    current = _bundle_platform_state(bundle)

    result.removed.extend(
        _remove_mcp_servers(
            platform,
            set(previous.mcp_servers) - set(current.mcp_servers),
            scope=scope,
            dry_run=dry_run,
        )
    )
    result.removed.extend(
        _remove_hooks(
            platform,
            set(previous.hooks) - set(current.hooks),
            scope=scope,
            dry_run=dry_run,
        )
    )
    result.removed.extend(
        _remove_skills(
            platform,
            set(previous.skills) - set(current.skills),
            scope=scope,
            dry_run=dry_run,
        )
    )
    return result, current
