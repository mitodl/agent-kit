"""Read-merge-write orchestration: turns a ``RegistrationBundle`` of canonical
model instances into on-disk config changes for one or all registry
platforms. Generalizes ``witan/setup.py``'s ``install_<agent>()`` functions
(§4.1 of the design spec).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import registry
from .installers import install_skills
from .jsonio import load_json_object, write_json
from .models import (
    CapabilityScope,
    DeclarativeHook,
    Hook,
    LspServer,
    McpServer,
    MergeStrategy,
    PluginRegistration,
    Scope,
    ScopeTarget,
    SkillSource,
)


@dataclass
class InstallResult:
    platform: str
    written: list[Path] = field(
        default_factory=list
    )  # files actually written (empty when dry_run)
    planned: list[Path] = field(
        default_factory=list
    )  # files that would be written (always populated)
    skipped: list[tuple[Path, str]] = field(default_factory=list)  # (path, reason)


@dataclass
class RegistrationBundle:
    """What a consumer (e.g. witan) wants installed, in canonical-model terms."""

    mcp_servers: dict[str, McpServer] = field(default_factory=dict)
    hooks: list[Hook] = field(default_factory=list)
    skills: list[SkillSource] = field(default_factory=list)
    lsp_servers: dict[str, LspServer] = field(
        default_factory=dict
    )  # unpopulated in v1 callers
    instructions: str | None = None  # unpopulated in v1 callers


def _resolve_target(capability: CapabilityScope, scope: Scope) -> ScopeTarget | None:
    return capability.global_ if scope == Scope.GLOBAL else capability.project


def _navigate(data: dict, key_path: tuple[str, ...]) -> dict:
    node = data
    for key in key_path:
        node = node.setdefault(key, {})
    return node


def _merge_into(
    container: dict, key: str, value: dict, strategy: MergeStrategy
) -> None:
    if strategy == MergeStrategy.OVERRIDE_BY_KEY:
        container[key] = value
    elif strategy == MergeStrategy.DEEP_MERGE:
        container.setdefault(key, {}).update(value)
    else:
        raise NotImplementedError(
            f"merge strategy {strategy} not supported for keyed entries"
        )


def _default_serialize(server: McpServer) -> dict:
    return server.model_dump(
        mode="json", exclude={"kind", "approval"}, exclude_none=True
    )


def apply(
    platform_name: str,
    bundle: RegistrationBundle,
    *,
    scope: Scope = Scope.GLOBAL,
    dry_run: bool = False,
) -> InstallResult:
    platform = registry.get_platform(platform_name)
    result = InstallResult(platform=platform_name)

    if platform.mcp is not None and bundle.mcp_servers:
        target = _resolve_target(platform.mcp, scope)
        if target is not None:
            cfg = load_json_object(target.path)
            if cfg is None:
                result.skipped.append((target.path, "could not parse as a JSON object"))
            else:
                container = _navigate(cfg, target.key_path)
                serialize = platform.mcp_serialize or _default_serialize
                for name, server in bundle.mcp_servers.items():
                    _merge_into(
                        container, name, serialize(server), platform.mcp.merge_strategy
                    )
                result.planned.append(target.path)
                write_json(target.path, cfg, dry_run)
                if not dry_run:
                    result.written.append(target.path)

    if platform.hooks is not None and bundle.hooks:
        declarative = [h for h in bundle.hooks if isinstance(h, DeclarativeHook)]
        plugins = [h for h in bundle.hooks if isinstance(h, PluginRegistration)]

        if declarative and platform.hooks_merge is not None:
            target = _resolve_target(platform.hooks, scope)
            if target is not None:
                cfg = load_json_object(target.path)
                if cfg is None:
                    result.skipped.append(
                        (target.path, "could not parse as a JSON object")
                    )
                else:
                    platform.hooks_merge(cfg, declarative)
                    result.planned.append(target.path)
                    write_json(target.path, cfg, dry_run)
                    if not dry_run:
                        result.written.append(target.path)

        # A platform's hooks target is either a JSON file (declarative merge)
        # or a plugin-file directory, never both — only copy plugin files for
        # platforms that don't do declarative JSON merging.
        if plugins and platform.hooks_merge is None:
            target = _resolve_target(platform.hooks, scope)
            if target is not None:
                for plugin in plugins:
                    dest = target.path / plugin.entry_path.name
                    result.planned.append(dest)
                    if not dry_run:
                        target.path.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(plugin.entry_path, dest)
                        result.written.append(dest)

    if platform.skills is not None and bundle.skills:
        target = _resolve_target(platform.skills, scope)
        if target is not None:
            dest_dirs = (
                platform.skill_dest_dirs(target.path)
                if platform.skill_dest_dirs
                else [target.path]
            )
            dests = install_skills(bundle.skills, dest_dirs, dry_run)
            result.planned.extend(dests)
            if not dry_run:
                result.written.extend(dests)

    return result


def apply_all(
    bundle: RegistrationBundle,
    *,
    scope: Scope = Scope.GLOBAL,
    dry_run: bool = False,
) -> dict[str, InstallResult]:
    return {
        name: apply(name, bundle, scope=scope, dry_run=dry_run)
        for name in registry.detect_installed_platforms()
    }
