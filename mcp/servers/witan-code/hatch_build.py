"""Hatchling build hook: bundle the omnigraph binary for the current platform.

Downloads from GitHub releases during `pip install` / `uvx` wheel build and
places the binary at witan_code/_bin/omnigraph inside the package. OmnigraphClient
checks there first before falling back to a PATH lookup, so the MCP server
works out of the box after `uvx --from git+... witan-code` with no separate
install step.

Fails gracefully on unsupported platforms or network errors — the runtime
falls back to whatever `omnigraph` is on PATH (or raises a clear error).
"""

from __future__ import annotations

import platform
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_OMNIGRAPH_VERSION = "0.7.2"
_BASE_URL = (
    f"https://github.com/ModernRelay/omnigraph/releases/download/v{_OMNIGRAPH_VERSION}"
)
_ASSETS: dict[tuple[str, str], str] = {
    ("linux", "x86_64"): "omnigraph-linux-x86_64.tar.gz",
    ("darwin", "arm64"): "omnigraph-macos-arm64.tar.gz",
}


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        key = (platform.system().lower(), platform.machine().lower())
        asset = _ASSETS.get(key)
        if asset is None:
            self.app.display_warning(
                f"omnigraph: no pre-built binary for {key[0]}/{key[1]} — "
                "binary not bundled; install manually and ensure it is on PATH"
            )
            return

        pkg_bin = Path(self.root) / "witan_code" / "_bin"
        pkg_bin.mkdir(parents=True, exist_ok=True)
        dest = pkg_bin / "omnigraph"

        if dest.exists():
            self.app.display_info(f"omnigraph: already bundled at {dest}")
            return

        url = f"{_BASE_URL}/{asset}"
        self.app.display_info(f"omnigraph: downloading {url}")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / asset
                with (
                    urllib.request.urlopen(url, timeout=30) as resp,
                    open(archive, "wb") as fh,
                ):
                    fh.write(resp.read())
                with tarfile.open(archive) as tf:
                    for member in tf.getmembers():
                        name = member.name.split("/")[-1]
                        if name == "omnigraph" and not member.isdir():
                            f = tf.extractfile(member)
                            if f:
                                dest.write_bytes(f.read())
                            break
            if dest.exists():
                dest.chmod(0o755)
                self.app.display_info(f"omnigraph: bundled to {dest}")
            else:
                self.app.display_warning(
                    "omnigraph: binary not found in archive — install manually"
                )
        except Exception as exc:  # noqa: BLE001
            self.app.display_warning(
                f"omnigraph: download failed ({exc}) — "
                "binary not bundled; install manually and ensure it is on PATH"
            )
