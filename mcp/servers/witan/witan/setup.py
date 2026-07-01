"""Witan's registration bundle, plus the omnigraph binary installer.

Per-agent MCP/skill/hook installation itself lives in ``agent_config_kit``
(``apply``/``apply_all``) — this module only builds witan's own
``RegistrationBundle`` and keeps the omnigraph binary distribution logic,
which is witan-specific and out of agent-config-kit's scope.
"""

from __future__ import annotations

import platform
import shutil
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

_OMNIGRAPH_VERSION = "0.7.0"
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


def install_omnigraph(pkg_dir: Path, dry_run: bool) -> None:
    from rich.console import Console

    console = Console()

    local_bin = Path.home() / ".local" / "bin"
    dest = local_bin / "omnigraph"

    bundled = pkg_dir / "_bin" / "omnigraph"
    if bundled.exists():
        if not dry_run:
            local_bin.mkdir(parents=True, exist_ok=True)
            tmp_dest = dest.with_name(dest.name + ".tmp")
            try:
                shutil.copy2(bundled, tmp_dest)
                tmp_dest.chmod(0o755)
                tmp_dest.replace(dest)
            finally:
                tmp_dest.unlink(missing_ok=True)
        console.print(f"  [green]omnigraph[/green] (bundled) → {dest}")
    else:
        # Not bundled — git/dev install or unsupported platform at build time.
        # Download directly from GitHub releases.
        _download_omnigraph(dest, dry_run)
