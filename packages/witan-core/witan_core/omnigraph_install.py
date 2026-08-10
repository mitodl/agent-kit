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

THE OUTGOING BINARY IS KEPT, not overwritten into oblivion. omnigraph uses
strict single-version storage: a release that bumps the on-disk format makes
every store written by the old binary unopenable, and the only sanctioned
recovery (``witan migrate storage`` → :func:`witan.server.migrate_storage_format`)
has to *export with the old binary* first. An installer that replaced the
binary in place therefore deleted the one tool needed to rescue the data it had
just orphaned, and told the user to go find it again on GitHub. So an upgrade
sets the previous version aside as ``omnigraph-<version>`` beside ``dest``, and
:func:`preserved_binary` is how the migration path finds it.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
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
#: Anchored, and a full semver — so the sweep that prunes stale set-aside
#: binaries can never match something a user put on their own PATH by hand
#: (``omnigraph-dev``, ``omnigraph-patched``). Only what this module wrote.
_PRESERVED_RE = re.compile(r"^omnigraph-(\d+\.\d+\.\d+)$")


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


def default_install_path() -> Path:
    """Where :func:`install_omnigraph` puts the binary."""
    return Path.home() / ".local" / "bin" / "omnigraph"


def preserved_binary(dest: Path | None = None) -> Path | None:
    """The pre-upgrade binary this installer set aside, or ``None``.

    Named ``omnigraph-<version>`` beside ``dest`` by :func:`_preserve_outgoing`.
    The highest version wins if several somehow survive — the store being
    rescued was written by the binary that ran most recently, and that is the
    newest one that is not the current pin.

    Callers use this to answer "what can still read a store the current binary
    refuses?" — see :func:`witan.server.migrate_storage_format`.
    """
    target = dest or default_install_path()
    found: list[tuple[tuple[int, ...], Path]] = []
    for entry in target.parent.glob("omnigraph-*"):
        match = _PRESERVED_RE.match(entry.name)
        if match and entry.is_file() and os.access(entry, os.X_OK):
            parsed = tuple(int(part) for part in match.group(1).split("."))
            found.append((parsed, entry))
    return max(found)[1] if found else None


def install_omnigraph(dry_run: bool = False) -> None:
    """Fetch the pinned omnigraph release into ``~/.local/bin/``.

    Skips the download when a binary is already present and reports the
    pinned version via ``--version``, so re-running always converges on the
    current pin without refetching an already-correct binary.
    """
    _download_omnigraph(default_install_path(), dry_run)


def _preserve_outgoing(dest: Path, version: str | None, console) -> None:
    """Set the outgoing binary aside as ``omnigraph-<version>`` beside ``dest``.

    Copied rather than moved: the copy runs *before* the atomic replace, and a
    move would leave the user with no working ``omnigraph`` at all in the
    window between the two, or permanently if the replace then failed.

    Exactly one previous version is kept. A store can only have been written by
    the binary that last wrote it, so a pile of older ones is dead weight (tens
    of MB each) and an ambiguity ``preserved_binary`` would have to guess its
    way out of. Only names this module wrote are swept — see
    ``_PRESERVED_RE``.

    Best-effort: failing to set the old binary aside must not abort an
    otherwise-working upgrade, so an ``OSError`` here warns and returns rather
    than raising. The user is left exactly where they were before this
    function existed, which is survivable; a failed install is not.
    """
    if not version or version == _OMNIGRAPH_VERSION or not dest.is_file():
        return
    keep = dest.with_name(f"omnigraph-{version}")
    try:
        shutil.copy2(dest, keep)
        keep.chmod(0o755)
    except OSError as exc:
        console.print(
            f"  [yellow]omnigraph[/yellow] — could not set v{version} aside "
            f"({exc}); `witan migrate storage` will need it passed by hand"
        )
        return
    console.print(f"  [dim]omnigraph[/dim] — previous v{version} kept at {keep}")
    for stale in dest.parent.glob("omnigraph-*"):
        if stale != keep and _PRESERVED_RE.match(stale.name):
            try:
                stale.unlink()
            except OSError:
                # Pruning is advisory. A stale copy left behind wastes disk;
                # aborting the sweep over it would waste the upgrade.
                pass


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

    # Read once and carry it: this is both the skip check and, further down,
    # the name the outgoing binary is set aside under. Re-reading after the
    # download would be reading the *new* binary.
    installed = _installed_version(dest)
    if installed == _OMNIGRAPH_VERSION:
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
            # Before the replace, never after: `replace` is what destroys the
            # old binary, and after it there is nothing left to preserve.
            _preserve_outgoing(dest, installed, console)
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
