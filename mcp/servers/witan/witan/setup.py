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
    hooks: list[Hook] = [
        DeclarativeHook(
            event=HookEvent.USER_PROMPT_SUBMIT, command="witan inject-context"
        ),
        DeclarativeHook(event=HookEvent.STOP, command="witan session-checkpoint"),
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
