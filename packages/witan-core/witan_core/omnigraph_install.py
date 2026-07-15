"""The omnigraph binary installer, shared by both witan servers.

Neither server bundles the omnigraph binary at build time; ``witan setup`` /
``witan-code setup`` fetch the pinned release into ``~/.local/bin/`` at
install/runtime instead, so every install converges on the same version.

``_OMNIGRAPH_VERSION`` was previously duplicated verbatim in
``witan/setup.py`` and ``witan-code/setup.py`` and kept in lockstep by a
Renovate custom manager spanning both files. Now that it lives here once, the
custom manager targets this single file and the lockstep hack is gone.

``rich`` is imported lazily inside ``_download_omnigraph`` so merely importing
this module stays dependency-free; only actually running an install needs it
(both servers already depend on ``rich``).
"""

from __future__ import annotations

import platform
import re
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

_OMNIGRAPH_VERSION = "0.8.1"
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
    try:
        from rich.console import Console
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise RuntimeError(
            "the omnigraph installer needs `rich` for its progress output; "
            "install it via the witan-core[cli] extra (both servers already "
            "depend on rich, so this only bites a bare witan-core install)."
        ) from exc

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
