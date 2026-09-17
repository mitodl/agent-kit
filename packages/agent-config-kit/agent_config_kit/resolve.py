"""Zero-arg ``agent-kit apply``/``validate`` manifest resolution (spec
§6.3/§7.2, decisions O2/S2).

Resolves which manifest (and default profiles/write scope) a zero-arg
invocation should use, in O2 order:

1. explicit CLI flags — handled by the caller before reaching this module.
2. a repo-local ``agent-config.toml`` at the repo root.
3. ``[[org]]`` match against the git remote (spec §8, decision O1).
4. the longest matching ``[[scope]]`` ``match_prefix``.
5. ``default_manifest``.

First hit wins for *which* manifest; that source's profiles/write-scope
travel with it (overridable by ``--profile``/``--scope`` at the CLI layer).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import GlobalConfig, OrgConfig, ScopeConfig
from .fetch import fetch_remote, is_remote_uri
from .manifest import merge_manifest_data, validate_raw_shapes
from .models import Scope

_GITHUB_OWNER_RE = re.compile(r"github\.com[:/]([^/]+)/")


@dataclass
class ResolvedManifest:
    path: Path
    # `None` -> this source carries no profile opinion of its own (the
    # repo-local case); fall through to the manifest's own
    # `[options].default_profiles`. A `list[str]` -- even `[]` -- is a real
    # override from `[[scope]]`/`[[org]]`/`default_profiles` and is used
    # verbatim ("profile is taken from the same source", spec §7.2), so an
    # explicitly-empty source list means "apply the whole manifest", not
    # "no opinion".
    profiles: list[str] | None
    write_scope: Scope | None
    source: str
    # Combined, still-raw (unvalidated) config-overlay fragment for this
    # invocation — global `[overlay]` plus whichever `[[org]]`/`[[scope]]`
    # entry's own `overlay` matched, if this source came from one (spec
    # `agent-config-kit-config-overlay-spec.md` §9 "Part A"). Empty dict
    # when neither contributed anything. `overlay_source` names which
    # layer(s) did, for the apply-output visibility line (I5); `""` when
    # `overlay` is empty.
    overlay: dict = field(default_factory=dict)
    overlay_source: str = ""


def find_repo_root(start: Path) -> Path | None:
    """Walk upward from ``start`` looking for a ``.git`` directory/file.
    ``None`` outside any git repo — an ordinary fall-through to the next O2
    step, not an error."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _run_git(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _parse_github_owner(remote_url: str) -> str | None:
    if match := _GITHUB_OWNER_RE.search(remote_url):
        return match.group(1)
    return None


def detect_org(repo_root: Path) -> str | None:
    """``git remote get-url origin`` (falling back to other remotes if
    there's no ``origin``), owner parsed from ``github.com[:/]<owner>/...``
    — covers both the SSH (``git@github.com:owner/repo.git``) and HTTPS
    (``https://github.com/owner/repo.git``) remote forms. No network, no
    ``gh`` dependency (O1) — every failure mode (``git`` not on ``PATH``,
    not a git repo, no remotes, no ``github.com`` remote) degrades to
    ``None``, an ordinary O2 fall-through rather than an error."""
    if shutil.which("git") is None:
        return None

    remote_names = (_run_git(["remote"], repo_root) or "").splitlines()
    if not remote_names:
        return None
    if "origin" in remote_names:
        remote_names = ["origin", *(n for n in remote_names if n != "origin")]

    for name in remote_names:
        url = _run_git(["remote", "get-url", name], repo_root)
        if url and (owner := _parse_github_owner(url)):
            return owner
    return None


def _match_org(owner: str, orgs: list[OrgConfig]) -> OrgConfig | None:
    for org_cfg in orgs:
        if org_cfg.name.lower() == owner.lower():
            return org_cfg
    return None


def default_manifest_cache_dir() -> Path:
    """Where a remote (``https://``/``git+``) top-level manifest resolved
    from the global config is fetched/cached — there's no local manifest
    directory to nest a ``.agent-config-kit-cache`` next to yet, unlike
    ``manifest.py``'s ``default_cache_dir`` for nested skill/hook sources."""
    xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg_cache_home) if xdg_cache_home else Path.home() / ".cache"
    return base / "agent-config-kit" / "manifests"


def _materialize(manifest_ref: str, *, cache_dir: Path) -> Path:
    if is_remote_uri(manifest_ref):
        return fetch_remote(manifest_ref, cache_dir)
    return Path(manifest_ref).expanduser()


def resolve_overlay(
    config: GlobalConfig,
    config_path: Path,
    *,
    org_match: OrgConfig | None = None,
    scope_match: ScopeConfig | None = None,
) -> tuple[dict, str]:
    """Combine config.toml's global ``[overlay]`` with the matched
    ``[[org]]``/``[[scope]]`` entry's own ``overlay`` (I3) — global wins a
    key collision between the two overlay layers. ``org_match`` and
    ``scope_match`` are never both given (O2's own branches are mutually
    exclusive). Returns the combined raw dict — still unvalidated and with
    paths unresolved, ``manifest.load_overlay_bundle`` does that — plus a
    short description of which layer(s) contributed, for the I5
    apply-output visibility line. ``({}, "")`` when neither has anything."""
    matched = (org_match.overlay if org_match else None) or (
        scope_match.overlay if scope_match else None
    )
    matched_label = None
    if matched and org_match:
        matched_label = f"org {org_match.name!r}"
    elif matched and scope_match:
        matched_label = f"scope {scope_match.match_prefix!r}"

    parts = [p for p in (matched_label, "global" if config.overlay else None) if p]
    if not parts:
        return {}, ""
    # merge_manifest_data isn't defensive about shape any more than the
    # include-merge machinery it's shared with is — a malformed layer (e.g.
    # `hooks` not a list) must fail with a clean ManifestError here, before
    # the merge touches it, not a raw TypeError/AttributeError from deep
    # inside `_merge_hooks`.
    if matched:
        validate_raw_shapes(matched, config_path)
    if config.overlay:
        validate_raw_shapes(config.overlay, config_path)
    if matched and config.overlay:
        combined = merge_manifest_data(matched, config.overlay, config_path)
    else:
        combined = matched or config.overlay
    return combined, " + ".join(parts)


def _longest_scope_match(cwd: Path, scopes: list[ScopeConfig]) -> ScopeConfig | None:
    """Longest matching ``match_prefix``, matched at directory-component
    boundaries (not ``str.startswith``, which would wrongly match a sibling
    like ``~/code/mit-backup`` against a ``~/code/mit`` prefix)."""
    best: ScopeConfig | None = None
    best_parts = -1
    for scope_cfg in scopes:
        prefix = Path(scope_cfg.match_prefix).expanduser().resolve()
        if cwd != prefix and prefix not in cwd.parents:
            continue
        if len(prefix.parts) > best_parts:
            best, best_parts = scope_cfg, len(prefix.parts)
    return best


def resolve_zero_arg_manifest(
    cwd: Path,
    config: GlobalConfig,
    config_path: Path,
    *,
    cache_dir: Path | None = None,
) -> ResolvedManifest | None:
    """O2 steps 2-5 (step 1, explicit CLI flags, never reaches this
    function). ``None`` means nothing resolved — the caller reports that as
    a clear, non-crashing error rather than this function raising.

    ``config_path`` is ``config.toml``'s own path (whether or not the file
    actually exists) — needed only for the overlay feature's I8 path
    anchor and error messages, not for anything O2 itself already did
    without it."""
    resolved_cache_dir = (
        cache_dir if cache_dir is not None else default_manifest_cache_dir()
    )
    resolved_cwd = cwd.resolve()

    repo_root = find_repo_root(resolved_cwd)
    if repo_root is not None:
        local_manifest = repo_root / "agent-config.toml"
        if local_manifest.is_file():
            overlay, overlay_source = resolve_overlay(config, config_path)
            return ResolvedManifest(
                path=local_manifest,
                profiles=None,
                write_scope=None,
                source=f"repo-local manifest at {local_manifest}",
                overlay=overlay,
                overlay_source=overlay_source,
            )

        if (owner := detect_org(repo_root)) and (
            org_match := _match_org(owner, config.org)
        ):
            overlay, overlay_source = resolve_overlay(
                config, config_path, org_match=org_match
            )
            return ResolvedManifest(
                path=_materialize(org_match.manifest, cache_dir=resolved_cache_dir),
                profiles=org_match.profiles,
                write_scope=None,
                source=f"org {owner!r}",
                overlay=overlay,
                overlay_source=overlay_source,
            )

    if scope_match := _longest_scope_match(resolved_cwd, config.scope):
        overlay, overlay_source = resolve_overlay(
            config, config_path, scope_match=scope_match
        )
        return ResolvedManifest(
            path=_materialize(scope_match.manifest, cache_dir=resolved_cache_dir),
            profiles=scope_match.profiles,
            write_scope=scope_match.write_scope,
            source=f"scope prefix {scope_match.match_prefix!r}",
            overlay=overlay,
            overlay_source=overlay_source,
        )

    if config.default_manifest:
        overlay, overlay_source = resolve_overlay(config, config_path)
        return ResolvedManifest(
            path=_materialize(config.default_manifest, cache_dir=resolved_cache_dir),
            profiles=config.default_profiles,
            write_scope=None,
            source="default_manifest",
            overlay=overlay,
            overlay_source=overlay_source,
        )

    return None
