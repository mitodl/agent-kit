"""Witan's registration bundle, plus the omnigraph binary installer.

Per-agent MCP/skill/hook installation itself lives in ``agent_config_kit``
(``apply``/``apply_all``) — this module only builds witan's own
``RegistrationBundle`` and keeps the omnigraph binary distribution logic,
which is witan-specific and out of agent-config-kit's scope.
"""

from __future__ import annotations

import platform
from pathlib import Path

from agent_config_kit import (
    DeclarativeHook,
    Hook,
    HookEvent,
    PluginRegistration,
    RegistrationBundle,
    SkillSource,
    StdioServer,
)

_WITAN_ARGS = [
    "--from",
    "git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan",
    "--with",
    "git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan-code",
    "witan",
    "serve",
]


def witan_bundle(pkg_dir: Path, author: str) -> RegistrationBundle:
    skills_dir = pkg_dir / "skills"
    skills = (
        [
            SkillSource(name=d.name, skill_md_path=d / "SKILL.md")
            for d in sorted(skills_dir.iterdir())
            if (d / "SKILL.md").exists()
        ]
        if skills_dir.is_dir()
        else []
    )

    pi_ext_dir = pkg_dir / "extensions" / "pi"
    # These hooks run on the prompt/stop critical path and do git + graph I/O, so
    # they carry a timeout: a hung git or graph read must degrade to no context,
    # never stall the agent. Matches Pi's 5s cap on the equivalent extension.
    hooks: list[Hook] = [
        DeclarativeHook(
            event=HookEvent.USER_PROMPT_SUBMIT,
            command="witan inject-context",
            timeout_seconds=5,
        ),
        DeclarativeHook(
            event=HookEvent.STOP,
            command="witan session-checkpoint",
            timeout_seconds=5,
        ),
    ]
    if pi_ext_dir.is_dir():
        hooks.extend(
            PluginRegistration(entry_path=f)
            for f in sorted(pi_ext_dir.iterdir())
            if f.suffix == ".ts"
        )

    return RegistrationBundle(
        mcp_servers={
            "witan": StdioServer(
                command="uvx", args=_WITAN_ARGS, env={"WITAN_AUTHOR": author}
            )
        },
        skills=skills,
        hooks=hooks,
    )


# ── Legacy hook migration ─────────────────────────────────────────────────────
# Older docs told users to register the workflow hooks as
# ``bash ~/.claude/hooks/workflow-context-inject.sh`` (a wrapper that just calls
# ``witan inject-context``). ``witan setup`` now registers the bare command, but
# Claude's hook dedup keys on the exact command string, so a pre-existing wrapper
# entry survives alongside the bare one and the context block prints twice. Prune
# any wrapper entry so the bare command is the single source of truth.

_LEGACY_HOOK_MARKERS = (
    "workflow-context-inject.sh",
    "workflow-session-checkpoint.sh",
)


def prune_legacy_hook_entries(settings: dict) -> bool:
    """Remove legacy ``.sh``-wrapper workflow-hook registrations from a Claude
    ``settings.json`` dict, in place. Returns ``True`` if anything changed.

    Matches on the wrapper script basename (an unambiguous witan-legacy marker),
    so it catches the ``bash ~/.claude/hooks/…`` and ``$REPO/configs/hooks/…``
    forms alike. Idempotent; leaves the bare ``witan …`` command entries and all
    non-witan hooks untouched. Drops a matcher entry entirely once it has no
    remaining hooks rather than leaving an empty ``"hooks": []`` behind.
    """
    hooks_section = settings.get("hooks")
    if not isinstance(hooks_section, dict):
        return False
    changed = False
    for event_name, entries in list(hooks_section.items()):
        if not isinstance(entries, list):
            continue
        kept: list = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                kept.append(entry)
                continue
            remaining = [
                h
                for h in entry["hooks"]
                if not (
                    isinstance(h, dict)
                    and any(m in (h.get("command") or "") for m in _LEGACY_HOOK_MARKERS)
                )
            ]
            if len(remaining) != len(entry["hooks"]):
                changed = True
            if remaining:
                kept.append({**entry, "hooks": remaining})
        hooks_section[event_name] = kept
    return changed


# ── Omnigraph binary ──────────────────────────────────────────────────────────
# Witan's own binary-distribution concern — explicitly out of agent-config-kit's
# scope (spec §3).
#
# No build-time bundling — `witan setup` always fetches the pinned release
# directly, so every install/re-run converges on the same version instead of
# a build hook and this module silently drifting apart (see the identical
# constant in witan-code/witan_code/setup.py, kept in lockstep by the
# omnigraph-version customManager in renovate.json — a single Renovate PR
# bumps both).

_OMNIGRAPH_VERSION = "0.8.0"
_OMNIGRAPH_ASSETS: dict[tuple[str, str], str] = {
    ("linux", "x86_64"): "omnigraph-linux-x86_64.tar.gz",
    ("darwin", "arm64"): "omnigraph-macos-arm64.tar.gz",
}


def _download_omnigraph(dest: Path, dry_run: bool) -> None:
    import tarfile
    import tempfile
    import urllib.request

    from rich.console import Console

    console = Console()

    key = (platform.system().lower(), platform.machine().lower())
    asset = _OMNIGRAPH_ASSETS.get(key)
    if asset is None:
        console.print(
            f"  [yellow]omnigraph[/yellow] — no pre-built binary for"
            f" {key[0]}/{key[1]}; install manually"
        )
        return

    url = (
        f"https://github.com/ModernRelay/omnigraph/releases/download"
        f"/v{_OMNIGRAPH_VERSION}/{asset}"
    )
    console.print(f"  downloading omnigraph v{_OMNIGRAPH_VERSION} …")

    if dry_run:
        console.print(f"  [green]omnigraph[/green] → {dest} [dim](dry-run)[/dim]")
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_dest = dest.with_name(dest.name + ".tmp")
    try:
        extracted = False
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / asset
            try:
                with (
                    urllib.request.urlopen(url, timeout=60) as resp,
                    open(archive, "wb") as fh,
                ):
                    fh.write(resp.read())
            except Exception as exc:  # noqa: BLE001
                console.print(
                    f"  [red]omnigraph download failed[/red] ({exc}); install manually"
                )
                return
            with tarfile.open(archive) as tf:
                for member in tf.getmembers():
                    if member.name.split("/")[-1] == "omnigraph" and not member.isdir():
                        f = tf.extractfile(member)
                        if f:
                            tmp_dest.write_bytes(f.read())
                            extracted = True
                        break
        if extracted:
            tmp_dest.chmod(0o755)
            tmp_dest.replace(dest)
            console.print(f"  [green]omnigraph[/green] → {dest}")
        else:
            console.print(
                "  [red]omnigraph[/red] — binary not found in archive; install manually"
            )
    except Exception as exc:  # noqa: BLE001
        console.print(
            f"  [red]omnigraph download failed[/red] ({exc}); install manually"
        )
    finally:
        tmp_dest.unlink(missing_ok=True)


def install_default_config(dry_run: bool) -> None:
    """Write a starter ``config.toml`` if one doesn't already exist.

    Unlike the omnigraph binary (always re-fetched to the current pin), a
    config file is user-owned once created — never overwritten by a re-run,
    so `witan setup` can't clobber edits the user has already made.
    """
    from rich.console import Console

    from . import config as cfg_module

    console = Console()
    dest = cfg_module.DEFAULT_CONFIG_PATH

    if dest.exists():
        console.print(f"  [dim]config.toml[/dim] — {dest} already exists, skipping")
        return
    if dry_run:
        console.print(f"  [green]config.toml[/green] → {dest} [dim](dry-run)[/dim]")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(cfg_module.default_config_toml())
    console.print(f"  [green]config.toml[/green] → {dest}")


def install_omnigraph(dry_run: bool) -> None:
    """Fetch the pinned omnigraph release into ``~/.local/bin/``.

    Always re-downloads rather than skipping when a binary is already
    present — `witan setup`'s own docstring promises "re-run after every
    upgrade to refresh installed files," and always converging on the
    current pin is what prevents a machine being stuck on a stale binary
    (the exact failure mode a build-time-only bundle produced before).
    """
    local_bin = Path.home() / ".local" / "bin"
    dest = local_bin / "omnigraph"
    _download_omnigraph(dest, dry_run)
