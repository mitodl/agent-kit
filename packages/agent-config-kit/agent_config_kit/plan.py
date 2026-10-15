"""Read-merge-write orchestration: turns a ``RegistrationBundle`` of canonical
model instances into on-disk config changes for one or all registry
platforms. Generalizes ``witan/setup.py``'s ``install_<agent>()`` functions
(§4.1 of the design spec).
"""

from __future__ import annotations

import copy
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import registry
from .configio import load_config_object, write_config
from .installers import _clear_symlink, _ensure_dest_dir, install_skills
from .jsonio import json_diff
from .models import (
    SKILL_DIR_PLACEHOLDER,
    AgentPlatform,
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


class HookSkillError(Exception):
    """A hook names a ``skill`` this run won't install for the platform, so
    its ``{skill_dir}`` has nothing to point at."""


@dataclass(frozen=True)
class Prerequisite:
    """Something a platform needs installed before the entries this run
    planned for it take effect (``AgentPlatform.mcp_conditional_on``) — e.g.
    Pi reads MCP servers only while its built-in MCP or pi-mcp-adapter is
    loaded.

    ``satisfied`` is the platform's read-only preflight result: ``True``
    when it looks installed, ``False`` when it looks missing, ``None`` when
    the platform has no reliable check. ``detail`` is the preflight's
    where-found or how-to-fix text (empty without a check)."""

    capability: str  # e.g. "mcp"
    requirement: str
    satisfied: bool | None = None
    detail: str = ""

    @property
    def needs_attention(self) -> bool:
        return self.satisfied is not True

    def message(self, *, dry_run: bool) -> str:
        verb = "would be written" if dry_run else "were written"
        head = f"{self.capability.upper()} entries {verb}; this platform {self.requirement}."
        if self.satisfied is True:
            return f"{head} Prerequisite found: {self.detail}."
        return f"{head} {self.detail or 'Could not verify it is installed.'}"


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
    removed: list[Path | str] = field(
        default_factory=list
    )  # entries pruned by `apply --prune` (see prune.py); always empty otherwise
    diffs: list[tuple[Path, str]] = field(default_factory=list)
    # (path, unified diff) for each JSON target actually changed by this run —
    # computed from the before/after in-memory dicts before `write_config` is
    # called, so it's populated the same way under `dry_run` as for a real
    # write. Only JSON-merge targets (MCP servers, declarative hooks) produce
    # one; skill/plugin-file copies are reported via `planned`/`written`
    # instead, since a copied file has no meaningful in-place diff.
    prerequisites: list[Prerequisite] = field(default_factory=list)
    # One entry per capability this run planned entries for whose platform
    # declares a prerequisite (``AgentPlatform.mcp_conditional_on``),
    # populated identically under ``dry_run`` — informational, never a
    # reason to skip the write.
    notices: list[str] = field(default_factory=list)
    # One line per declarative hook this run added or changed on a platform
    # that won't run it until the user approves it
    # (``AgentPlatform.hooks_approval``). Populated under ``dry_run`` too.


@dataclass
class RegistrationBundle:
    """What a consumer (e.g. witan) wants installed, in canonical-model terms."""

    mcp_servers: dict[str, McpServer] = field(default_factory=dict)
    # Per-platform overrides, merged over ``mcp_servers`` by key for the named
    # platform only. The right way to *launch* a server can differ per platform
    # even when the server itself is identical: a platform whose hooks already
    # require a CLI on PATH should point its MCP entry at that same CLI, so both
    # surfaces run one install; a platform with no CLI of its own needs a
    # self-contained launcher. See ``witan.setup.witan_bundle``.
    mcp_servers_by_platform: dict[str, dict[str, McpServer]] = field(
        default_factory=dict
    )
    hooks: list[Hook] = field(default_factory=list)
    skills: list[SkillSource] = field(default_factory=list)
    lsp_servers: dict[str, LspServer] = field(
        default_factory=dict
    )  # unpopulated in v1 callers
    instructions: str | None = None  # unpopulated in v1 callers


def _resolve_target(capability: CapabilityScope, scope: Scope) -> ScopeTarget | None:
    return capability.global_ if scope == Scope.GLOBAL else capability.project


def _navigate(data: dict, key_path: tuple[str, ...]) -> dict:
    """Walk/create ``key_path`` in ``data``, coercing non-dict values found
    along the way (e.g. a hand-edited config with ``"hooks": []``) to an
    empty object rather than raising on the next ``dict`` operation."""
    node = data
    for key in key_path:
        if not isinstance(node.get(key), dict):
            node[key] = {}
        node = node[key]
    return node


def _merge_into(
    container: dict, key: str, value: dict, strategy: MergeStrategy
) -> None:
    if strategy == MergeStrategy.OVERRIDE_BY_KEY:
        container[key] = value
    elif strategy == MergeStrategy.DEEP_MERGE:
        if not isinstance(container.get(key), dict):
            container[key] = {}
        container[key].update(value)
    else:
        raise NotImplementedError(
            f"merge strategy {strategy} not supported for keyed entries"
        )


def _default_serialize(server: McpServer) -> dict:
    return server.model_dump(
        mode="json", exclude={"kind", "approval"}, exclude_none=True
    )


def skill_dest_dirs(platform: AgentPlatform, target: Path) -> list[Path]:
    """Where ``platform`` installs skills under its skills ``target``."""
    return platform.skill_dest_dirs(target) if platform.skill_dest_dirs else [target]


def hook_changes(platform: AgentPlatform, cfg: dict, hook: DeclarativeHook) -> bool:
    """Whether merging ``hook`` would change ``cfg``. ``hooks_merge`` is
    dedup-based, so an already-present, unchanged hook is a no-op."""
    assert platform.hooks_merge is not None
    probe = copy.deepcopy(cfg)
    platform.hooks_merge(probe, [hook])
    return probe != cfg


def _skill_dir(
    platform_name: str,
    platform: AgentPlatform,
    hook: DeclarativeHook,
    bundle: RegistrationBundle,
    scope: Scope,
) -> Path:
    if hook.skill not in {s.name for s in bundle.skills}:
        raise HookSkillError(
            f"{platform_name}: hook {hook.command!r} references skill "
            f"{hook.skill!r}, which this run does not install"
        )
    target = _resolve_target(platform.skills, scope) if platform.skills else None
    if target is None:
        raise HookSkillError(
            f"{platform_name}: hook {hook.command!r} references skill "
            f"{hook.skill!r}, but {platform.name} installs no skills at "
            f"{scope.value} scope"
        )
    return skill_dest_dirs(platform, target.path)[0] / hook.skill


def platform_hooks(
    platform_name: str, bundle: RegistrationBundle, scope: Scope
) -> list[Hook]:
    """The hooks in ``bundle`` that ``platform_name`` actually writes:
    declarative hooks for a platform that merges them into a config file,
    plugin files for one that doesn't. A declarative hook's ``{skill_dir}``
    is replaced with where this platform installs its ``skill``, so one
    manifest entry serves every platform. Raises ``HookSkillError`` when
    that skill isn't installed for this platform in this run."""
    platform = registry.get_platform(platform_name)
    if platform.hooks is None or _resolve_target(platform.hooks, scope) is None:
        return []
    if platform.hooks_merge is None:
        return [h for h in bundle.hooks if isinstance(h, PluginRegistration)]
    hooks: list[Hook] = []
    for hook in bundle.hooks:
        if not isinstance(hook, DeclarativeHook):
            continue
        if hook.skill is not None:
            skill_dir = _skill_dir(platform_name, platform, hook, bundle, scope)
            hook = hook.model_copy(
                update={
                    "command": hook.command.replace(
                        SKILL_DIR_PLACEHOLDER, str(skill_dir)
                    ),
                    "skill": None,
                }
            )
        hooks.append(hook)
    return hooks


def check_hook_skills(
    bundle: RegistrationBundle, targets: list[str], scope: Scope
) -> None:
    """Resolve every target's ``{skill_dir}`` hooks before anything is
    written, so a hook naming a skill one platform won't install fails the
    run instead of leaving the platforms before it half-applied."""
    for name in targets:
        platform_hooks(name, bundle_for(name, bundle, targets, scope), scope)


def _mcp_prerequisite(platform: AgentPlatform, scope: Scope) -> Prerequisite:
    assert platform.mcp_conditional_on is not None
    if platform.mcp_prerequisite_check is None:
        return Prerequisite(capability="mcp", requirement=platform.mcp_conditional_on)
    satisfied, detail = platform.mcp_prerequisite_check(scope)
    return Prerequisite(
        capability="mcp",
        requirement=platform.mcp_conditional_on,
        satisfied=satisfied,
        detail=detail,
    )


def _approval_notices(
    platform: AgentPlatform,
    hooks: list[DeclarativeHook],
    before: dict,
    path: Path,
    dry_run: bool,
) -> list[str]:
    """One line per hook whose merge changes ``before`` (new, or e.g. a new
    timeout): an unchanged hook was already approved on an earlier run."""
    verb = "would be written" if dry_run else "was written"
    return [
        f"{hook.event.value} hook {hook.command!r} {verb} to {path}; "
        f"{platform.hooks_approval}"
        for hook in hooks
        if hook_changes(platform, before, hook)
    ]


def apply(
    platform_name: str,
    bundle: RegistrationBundle,
    *,
    scope: Scope = Scope.GLOBAL,
    dry_run: bool = False,
    force: bool = False,
    _json_state: dict[Path, tuple[dict, dict]] | None = None,
) -> InstallResult:
    """``_json_state``, if given, is populated with ``{path: (before, after)}``
    for each JSON target this call touches (the same before/after diffed into
    ``result.diffs``). It exists so ``prune.apply_with_prune`` can seed its own
    removal step from this call's in-memory ``after`` — rather than re-reading
    the file from disk, which under ``dry_run`` would still be this run's
    stale pre-merge content — and then rebuild the diff from the *whole*
    apply-then-prune lifecycle instead of just this merge step."""
    platform = registry.get_platform(platform_name)
    result = InstallResult(platform=platform_name)
    # Resolved before any write: a missing skill raises HookSkillError here
    # rather than after the MCP target is already written.
    hooks = platform_hooks(platform_name, bundle, scope)

    mcp_servers = {
        **bundle.mcp_servers,
        **bundle.mcp_servers_by_platform.get(platform_name, {}),
    }
    if platform.mcp is not None and mcp_servers:
        target = _resolve_target(platform.mcp, scope)
        if target is not None:
            cfg = load_config_object(target.path)
            if cfg is None:
                result.skipped.append(
                    (target.path, "could not parse as a config object")
                )
            else:
                before = copy.deepcopy(cfg)
                container = _navigate(cfg, target.key_path)
                serialize = platform.mcp_serialize or _default_serialize
                for name, server in mcp_servers.items():
                    _merge_into(
                        container, name, serialize(server), platform.mcp.merge_strategy
                    )
                result.planned.append(target.path)
                diff = json_diff(before, cfg)
                if diff:
                    result.diffs.append((target.path, diff))
                if _json_state is not None:
                    _json_state[target.path] = (before, cfg)
                write_config(target.path, cfg, dry_run)
                if not dry_run:
                    result.written.append(target.path)
                if platform.mcp_conditional_on is not None:
                    result.prerequisites.append(_mcp_prerequisite(platform, scope))

    if platform.hooks is not None and hooks:
        declarative = [h for h in hooks if isinstance(h, DeclarativeHook)]
        plugins = [h for h in hooks if isinstance(h, PluginRegistration)]

        if declarative and platform.hooks_merge is not None:
            target = _resolve_target(platform.hooks, scope)
            if target is not None:
                cfg = load_config_object(target.path)
                if cfg is None:
                    result.skipped.append(
                        (target.path, "could not parse as a config object")
                    )
                else:
                    before = copy.deepcopy(cfg)
                    platform.hooks_merge(cfg, declarative)
                    if platform.hooks_approval is not None:
                        result.notices.extend(
                            _approval_notices(
                                platform, declarative, before, target.path, dry_run
                            )
                        )
                    result.planned.append(target.path)
                    diff = json_diff(before, cfg)
                    if diff:
                        result.diffs.append((target.path, diff))
                    if _json_state is not None:
                        _json_state[target.path] = (before, cfg)
                    write_config(target.path, cfg, dry_run)
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
                        _ensure_dest_dir(target.path, force=force)
                        _clear_symlink(dest, force=force)
                        shutil.copy2(plugin.entry_path, dest)
                        result.written.append(dest)

    if platform.skills is not None and bundle.skills:
        target = _resolve_target(platform.skills, scope)
        if target is not None:
            dest_dirs = skill_dest_dirs(platform, target.path)
            dests = install_skills(bundle.skills, dest_dirs, dry_run, force=force)
            result.planned.extend(dests)
            if not dry_run:
                result.written.extend(dests)

    return result


def skills_shadowed_by(
    platform_name: str,
    targets: list[str],
    scope: Scope,
    skills: list[SkillSource] | None = None,
) -> str | None:
    """Why a platform's own skill copy would duplicate one it already reads.

    Another target in this run is sufficient, but not necessary: a previous
    Codex run may already have installed this bundle in Pi's shared skill
    directory.  When the current bundle's skills are all there, retain that
    layout for Pi-only apply and validate runs too.
    """
    platform = registry.get_platform(platform_name)
    if platform.skills_also_reads is None:
        return None
    also_read = _resolve_target(platform.skills_also_reads, scope)
    if also_read is None:
        return None
    for other in targets:
        if other == platform_name:
            continue
        other_skills = registry.get_platform(other).skills
        target = _resolve_target(other_skills, scope) if other_skills else None
        if target is not None and target.path == also_read.path:
            return other
    if skills and all(
        (also_read.path / skill.name / "SKILL.md").is_file() for skill in skills
    ):
        return "the shared skill directory"
    return None


def bundle_for(
    platform_name: str, bundle: RegistrationBundle, targets: list[str], scope: Scope
) -> RegistrationBundle:
    """``bundle`` minus its skills when another targeted platform already
    installs them somewhere ``platform_name`` reads (``skills_shadowed_by``).
    Used for apply, prune and validate alike, so a prune run also removes
    the copies an earlier run installed before the overlap was handled."""
    if bundle.skills and skills_shadowed_by(
        platform_name, targets, scope, bundle.skills
    ):
        return replace(bundle, skills=[])
    return bundle


def apply_all(
    bundle: RegistrationBundle,
    *,
    scope: Scope = Scope.GLOBAL,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, InstallResult]:
    targets = registry.detect_installed_platforms()
    check_hook_skills(bundle, targets, scope)
    return {
        name: apply(
            name,
            bundle_for(name, bundle, targets, scope),
            scope=scope,
            dry_run=dry_run,
            force=force,
        )
        for name in targets
    }
