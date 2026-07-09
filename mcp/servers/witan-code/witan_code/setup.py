"""Witan-code's registration bundle, plus the omnigraph binary installer.

Per-agent MCP/skill/hook installation itself lives in ``agent_config_kit``
(``apply``/``apply_all``) — this module only builds witan-code's own
``RegistrationBundle`` and keeps the omnigraph binary distribution logic.

The bundle-building shape is copied from witan/witan/setup.py so the two
Layer packages stay independent — no cross-package imports, matching
OmnigraphClient's own docstring in graph.py. ``_OMNIGRAPH_VERSION`` here is
kept in lockstep with witan's copy by the omnigraph-version customManager in
renovate.json — a single Renovate PR bumps both.
"""

from __future__ import annotations

import platform
import re
import subprocess
import tarfile
import tempfile
import urllib.request
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

_WITAN_CODE_ARGS = [
    "--from",
    "git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan-code",
    "witan-code",
    "serve",
]

_OMNIGRAPH_VERSION = "0.8.0"
_OMNIGRAPH_ASSETS: dict[tuple[str, str], str] = {
    ("linux", "x86_64"): "omnigraph-linux-x86_64.tar.gz",
    ("darwin", "arm64"): "omnigraph-macos-arm64.tar.gz",
}
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


def _installed_version(dest: Path) -> str | None:
    """Return ``dest``'s reported version, or ``None`` if absent/unreadable.

    A hung, corrupted, or non-executable binary must degrade to "unknown
    version" (triggering a re-download) rather than crash `setup` —
    ``subprocess.TimeoutExpired`` is a ``SubprocessError``, not an
    ``OSError``, so both need catching, and a non-zero exit means the
    output isn't trustworthy version text even if something printed.
    """
    if not dest.exists():
        return None
    try:
        result = subprocess.run(
            [str(dest), "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    match = _VERSION_RE.search(result.stdout + result.stderr)
    return match.group(0) if match else None


def witan_code_bundle(
    pkg_dir: Path, author: str, *, binary: str = "witan-code"
) -> RegistrationBundle:
    """Build witan-code's ``RegistrationBundle``: MCP server, skill, hooks.

    Independent of ``witan``'s own bundle (``witan.setup.witan_bundle``) — a
    witan-code-only install (no witan) still gets the skill, hooks, and Pi
    extension via standalone ``witan-code setup``. When both packages are
    installed together and witan-code is importable, ``witan setup`` also
    folds this bundle in automatically (see ``witan.cli.setup_cmd``), so a
    single ``witan setup`` covers both; running ``witan-code setup``
    separately afterwards is harmless (each `apply()` call is an idempotent
    read-merge-write) but not required in that case.

    Parameters
    ----------
    binary: The command name hook entries invoke — ``"witan-code"`` for a
        standalone install (this function's default), or ``"witan code"``
        when ``witan.cli.setup_cmd`` folds this bundle into witan's own (the
        hooks then only need `witan` — with witan-code bundled in via
        ``--with`` — on PATH, not a separately installed `witan-code`).
    """
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
    # Bare CLI commands, no wrapper script — portable everywhere `binary`
    # installs (Windows included, where bash/setsid don't exist), matching
    # witan's own `witan inject-context`/`session-checkpoint` hooks. The
    # prompt-path timeouts mirror witan's: a hung git or store read must
    # degrade to no context/no compaction, never stall the agent.
    hooks: list[Hook] = [
        DeclarativeHook(
            event=HookEvent.SESSION_START,
            command=f"{binary} session-init",
        ),
        DeclarativeHook(
            event=HookEvent.POST_TOOL_USE,
            matcher="Edit|Write",
            command=f"{binary} reindex-hook",
        ),
        DeclarativeHook(
            event=HookEvent.USER_PROMPT_SUBMIT,
            command=f"{binary} inject-context",
            timeout_seconds=15,
        ),
        DeclarativeHook(
            event=HookEvent.STOP,
            command=f"{binary} checkpoint",
            timeout_seconds=15,
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
            "witan-code": StdioServer(
                command="uvx", args=_WITAN_CODE_ARGS, env={"WITAN_AUTHOR": author}
            )
        },
        skills=skills,
        hooks=hooks,
    )


def install_omnigraph(dry_run: bool = False) -> None:
    """Fetch the pinned omnigraph release into ``~/.local/bin/``.

    Skips the download when a binary is already present and reports the
    pinned version via ``--version``, so re-running always converges on the
    current pin without refetching an already-correct binary.
    """
    local_bin = Path.home() / ".local" / "bin"
    dest = local_bin / "omnigraph"
    _download_omnigraph(dest, dry_run)


def _download_omnigraph(dest: Path, dry_run: bool) -> None:
    from rich.console import Console

    console = Console()

    if _installed_version(dest) == _OMNIGRAPH_VERSION:
        console.print(
            f"  [dim]omnigraph[/dim] — {dest} already at v{_OMNIGRAPH_VERSION}, skipping"
        )
        return

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
