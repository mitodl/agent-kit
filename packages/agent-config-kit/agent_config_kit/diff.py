"""Non-mutating drift detection: computes what ``apply()`` would write for a
platform without touching disk. Reuses ``plan.py``'s target resolution and
each platform's own ``mcp_serialize``/``hooks_merge`` projections so there is
exactly one place that knows a platform's wire format (see
``docs/internals/design/agent-config-kit-cli-spec.md`` §4.2).

Deviates from that spec's sketch in one way: ``Drift`` has no single ``path``
field. A platform can write MCP servers, hooks, and skills to three different
files/dirs, so drift is reported per capability key (e.g.
``"mcp_servers:witan"``) rather than per file.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

from . import registry
from .installers import install_skills
from .jsonio import load_json_object
from .models import DeclarativeHook, PluginRegistration, Scope
from .plan import RegistrationBundle, _default_serialize, _navigate, _resolve_target


@dataclass
class Drift:
    platform: str
    missing_keys: list[str] = field(default_factory=list)  # in manifest, absent on disk
    mismatched_keys: list[str] = field(
        default_factory=list
    )  # in manifest, present on disk, different value
    missing_paths: list[Path] = field(
        default_factory=list
    )  # skills/plugin-hook files the manifest wants that aren't on disk
    unreadable_paths: list[Path] = field(
        default_factory=list
    )  # JSON targets that failed to parse — reported distinctly, not drift

    @property
    def has_drift(self) -> bool:
        return bool(self.missing_keys or self.mismatched_keys or self.missing_paths)


def _diff_mcp_servers(
    platform: registry.AgentPlatform,
    bundle: RegistrationBundle,
    scope: Scope,
    result: Drift,
) -> None:
    if platform.mcp is None or not bundle.mcp_servers:
        return
    target = _resolve_target(platform.mcp, scope)
    if target is None:
        return
    cfg = load_json_object(target.path)
    if cfg is None:
        result.unreadable_paths.append(target.path)
        return
    container = _navigate(cfg, target.key_path)
    serialize = platform.mcp_serialize or _default_serialize
    for name, server in bundle.mcp_servers.items():
        desired = serialize(server)
        if name not in container:
            result.missing_keys.append(f"mcp_servers:{name}")
        elif container[name] != desired:
            result.mismatched_keys.append(f"mcp_servers:{name}")


def _diff_hooks(
    platform: registry.AgentPlatform,
    bundle: RegistrationBundle,
    scope: Scope,
    result: Drift,
) -> None:
    if platform.hooks is None or not bundle.hooks:
        return
    declarative = [h for h in bundle.hooks if isinstance(h, DeclarativeHook)]
    plugins = [h for h in bundle.hooks if isinstance(h, PluginRegistration)]

    if declarative and platform.hooks_merge is not None:
        target = _resolve_target(platform.hooks, scope)
        if target is not None:
            cfg = load_json_object(target.path)
            if cfg is None:
                result.unreadable_paths.append(target.path)
            else:
                for hook in declarative:
                    # hooks_merge is idempotent/dedup-based: merging a single
                    # hook into an unmodified copy of cfg is a no-op if it's
                    # already present, and a mutation if it's missing — reuses
                    # the platform's own merge as the single source of truth
                    # instead of re-deriving its dedup key here.
                    probe = copy.deepcopy(cfg)
                    platform.hooks_merge(probe, [hook])
                    if probe != cfg:
                        result.missing_keys.append(
                            f"hooks:{hook.event.value}:{hook.command}"
                        )

    if plugins and platform.hooks_merge is None:
        target = _resolve_target(platform.hooks, scope)
        if target is not None:
            for plugin in plugins:
                dest = target.path / plugin.entry_path.name
                if not dest.exists():
                    result.missing_paths.append(dest)


def _diff_skills(
    platform: registry.AgentPlatform,
    bundle: RegistrationBundle,
    scope: Scope,
    result: Drift,
) -> None:
    if platform.skills is None or not bundle.skills:
        return
    target = _resolve_target(platform.skills, scope)
    if target is None:
        return
    dest_dirs = (
        platform.skill_dest_dirs(target.path)
        if platform.skill_dest_dirs
        else [target.path]
    )
    dests = install_skills(bundle.skills, dest_dirs, dry_run=True)
    result.missing_paths.extend(dest for dest in dests if not dest.exists())


def diff(
    platform_name: str, bundle: RegistrationBundle, *, scope: Scope = Scope.GLOBAL
) -> Drift:
    platform = registry.get_platform(platform_name)
    result = Drift(platform=platform_name)
    _diff_mcp_servers(platform, bundle, scope, result)
    _diff_hooks(platform, bundle, scope, result)
    _diff_skills(platform, bundle, scope, result)
    return result
