"""``ac-kit`` console script — gated behind the ``cli`` extra.

The base ``agent_config_kit`` package stays importable with only ``pydantic``
as a dependency (spec D3); this module is the only place ``cyclopts``/``rich``
are imported, and ``agent_config_kit/__init__.py`` never imports it. Running
the console script without the ``cli`` extra installed fails fast here with a
clear message instead of a bare traceback from deep inside cyclopts/rich.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import cyclopts
    from rich.console import Console
    from rich.table import Table
except ImportError as exc:
    sys.stderr.write(
        "ac-kit: the CLI requires the `cli` extra.\n"
        "Install it with: pip install 'agent-config-kit[cli]'\n"
        "(or: uv tool install 'agent-config-kit[cli]')\n"
    )
    raise SystemExit(1) from exc

from .diff import Drift
from .diff import diff as diff_bundle
from .manifest import ManifestError, load_manifest
from .models import Scope
from .plan import InstallResult
from .plan import apply as apply_bundle
from .plan import apply_all as apply_all_bundle
from .prune import (
    PlatformState,
    apply_with_prune,
    default_state_path,
    load_state,
    write_state,
)
from .registry import detect_installed_platforms, known_platforms

app = cyclopts.App(
    name="ac-kit",
    help=(
        "Apply and validate manifest-driven MCP server, skill, and hook "
        "registration across coding-agent platforms."
    ),
)
console = Console()


def _resolve_platforms(
    manifest_platforms: list[str] | None, cli_platforms: list[str] | None
) -> list[str] | None:
    """CLI ``--platform`` wins over the manifest's ``[options.platforms]``
    (spec §4: "explicit beats declarative")."""
    platforms = cli_platforms if cli_platforms else manifest_platforms
    if platforms is None:
        return None
    unknown = sorted(set(platforms) - set(known_platforms()))
    if unknown:
        console.print(
            f"[red]unknown platform(s): {', '.join(unknown)} "
            f"(known: {', '.join(known_platforms())})[/red]"
        )
        raise SystemExit(2)
    return platforms


def _report(results: dict[str, InstallResult], *, dry_run: bool) -> bool:
    """Print a rich table of each platform's result. Returns True if any
    platform had a skipped entry."""
    table = Table("platform", "planned" if dry_run else "written", "skipped", "removed")
    had_skipped = False
    for name, result in results.items():
        paths = result.planned if dry_run else result.written
        if result.skipped:
            had_skipped = True
        table.add_row(
            name,
            "\n".join(str(p) for p in paths) or "-",
            "\n".join(f"{p}: {reason}" for p, reason in result.skipped) or "-",
            "\n".join(str(r) for r in result.removed) or "-",
        )
    console.print(table)
    return had_skipped


@app.command(name="apply")
def apply_command(
    manifest: Path,
    *,
    scope: Scope | None = None,
    platform: list[str] | None = None,
    dry_run: bool = False,
    prune: bool = False,
    state_file: Path | None = None,
    cache_dir: Path | None = None,
) -> None:
    """Apply a manifest's MCP servers, hooks, and skills to one or more
    coding-agent platforms.

    Parameters
    ----------
    manifest
        Path to the manifest TOML file.
    scope
        Overrides the manifest's ``[options].scope`` for this run.
    platform
        Platform name to target; repeatable. Overrides the manifest's
        ``[options.platforms]`` and, if neither is given, every detected
        platform is targeted (same default as ``apply_all``).
    dry_run
        Report what would be written/removed without writing anything.
    prune
        Also remove entries that a previous ``apply --prune`` of this
        manifest wrote but that are no longer present in it. Opt-in only —
        a plain ``apply`` never removes anything, and a manifest with no
        prior recorded state (e.g. its first-ever ``--prune`` run) prunes
        nothing rather than guessing.
    state_file
        Where to read/write the prune state file. Defaults to
        ``<manifest>.lock.json``. Ignored unless ``--prune`` is given.
    cache_dir
        Where remote (``https://``/``git+``) skill/hook sources are fetched
        and cached. Defaults to ``.agent-config-kit-cache`` next to the
        manifest.
    """
    try:
        loaded = load_manifest(manifest, cache_dir=cache_dir)
    except ManifestError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(2) from exc

    resolved_scope = scope if scope is not None else loaded.options.scope
    platforms = _resolve_platforms(loaded.options.platforms, platform)

    if prune:
        target_platforms = (
            platforms if platforms is not None else detect_installed_platforms()
        )
        state_path = (
            state_file if state_file is not None else default_state_path(manifest)
        )
        states = load_state(state_path)
        results: dict[str, InstallResult] = {}
        for name in target_platforms:
            result, current_state = apply_with_prune(
                name,
                loaded.bundle,
                states.get(name, PlatformState()),
                scope=resolved_scope,
                dry_run=dry_run,
            )
            results[name] = result
            # A skipped target (e.g. unreadable JSON) means apply() didn't
            # actually write anything for this platform this run — recording
            # "current_state" as if it had would claim ownership over
            # entries never actually applied, so a later prune could remove
            # something this run never truly wrote. Leave the platform's
            # last-known-good state untouched instead.
            if not result.skipped:
                states[name] = current_state
        if not dry_run:
            write_state(state_path, manifest, states)
    elif platforms is not None:
        results = {
            name: apply_bundle(
                name, loaded.bundle, scope=resolved_scope, dry_run=dry_run
            )
            for name in platforms
        }
    else:
        results = apply_all_bundle(loaded.bundle, scope=resolved_scope, dry_run=dry_run)

    if _report(results, dry_run=dry_run):
        raise SystemExit(1)


def _report_drift(drifts: dict[str, Drift]) -> bool:
    """Print a rich table of each platform's drift. Returns True if any
    platform has drift or an unreadable target (the latter isn't drift per
    se, but must not exit 0 silently)."""
    table = Table("platform", "missing", "mismatched", "missing paths", "unreadable")
    had_issue = False
    for name, drift in drifts.items():
        if drift.has_drift or drift.unreadable_paths:
            had_issue = True
        table.add_row(
            name,
            "\n".join(drift.missing_keys) or "-",
            "\n".join(drift.mismatched_keys) or "-",
            "\n".join(str(p) for p in drift.missing_paths) or "-",
            "\n".join(str(p) for p in drift.unreadable_paths) or "-",
        )
    console.print(table)
    return had_issue


@app.command(name="validate")
def validate_command(
    manifest: Path,
    *,
    scope: Scope | None = None,
    platform: list[str] | None = None,
    cache_dir: Path | None = None,
) -> None:
    """Report drift between a manifest and each platform's on-disk config,
    without writing anything.

    Parameters
    ----------
    manifest
        Path to the manifest TOML file.
    scope
        Overrides the manifest's ``[options].scope`` for this run.
    platform
        Platform name to check; repeatable. Overrides the manifest's
        ``[options.platforms]`` and, if neither is given, every detected
        platform is checked (same default as ``apply_all``).
    cache_dir
        Where remote (``https://``/``git+``) skill/hook sources are fetched
        and cached. Defaults to ``.agent-config-kit-cache`` next to the
        manifest.
    """
    try:
        loaded = load_manifest(manifest, cache_dir=cache_dir)
    except ManifestError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(2) from exc

    resolved_scope = scope if scope is not None else loaded.options.scope
    platforms = _resolve_platforms(loaded.options.platforms, platform)
    if platforms is None:
        platforms = detect_installed_platforms()

    drifts = {
        name: diff_bundle(name, loaded.bundle, scope=resolved_scope)
        for name in platforms
    }

    if _report_drift(drifts):
        raise SystemExit(1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
