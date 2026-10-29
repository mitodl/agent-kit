#!/usr/bin/env -S uv run --quiet --package witan-core python
"""Install ``omnigraph-server`` from the pinned, checksummed omnigraph release.

``install_omnigraph`` extracts only the CLI, because that is all ``witan
setup`` needs. The served tests (``testsupport/omnigraph_server.py``, used by
witan's group-commit tests and witan-code's branch-view tests) start a real
``omnigraph-server`` from the same tarball, and without it they skip, so CI
never ran the default write path against a real server.
"""

import hashlib
import platform
import tarfile
import tempfile
import urllib.request
from pathlib import Path

import cyclopts

from witan_core.omnigraph_install import (
    _OMNIGRAPH_ASSET_SHA256,
    _OMNIGRAPH_ASSETS,
    _OMNIGRAPH_RELEASE_TAG,
    default_install_path,
)

SERVER_BINARY = "omnigraph-server"
DOWNLOAD_TIMEOUT_SECONDS = 120

app = cyclopts.App(help=__doc__)


@app.default
def main(dest: Path | None = None) -> None:
    """Fetch the pinned release, verify it, and extract ``omnigraph-server``.

    :param dest: where to put the binary. Defaults to beside the CLI
        (``~/.local/bin/omnigraph-server``).
    """
    dest = dest or default_install_path().with_name(SERVER_BINARY)
    asset = _OMNIGRAPH_ASSETS[(platform.system().lower(), platform.machine().lower())]
    url = (
        "https://github.com/ModernRelay/omnigraph/releases/download"
        f"/{_OMNIGRAPH_RELEASE_TAG}/{asset}"
    )
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / asset
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as resp:
            archive.write_bytes(resp.read())
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        expected = _OMNIGRAPH_ASSET_SHA256[asset]
        if actual != expected:
            msg = f"{asset}: expected sha256 {expected}, got {actual}"
            raise SystemExit(msg)
        with tarfile.open(archive) as tf:
            (member,) = (
                m
                for m in tf.getmembers()
                if m.name.split("/")[-1] == SERVER_BINARY and m.isfile()
            )
            extracted = tf.extractfile(member)
            assert extracted is not None
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(extracted.read())
    dest.chmod(0o755)
    print(f"{SERVER_BINARY} {_OMNIGRAPH_RELEASE_TAG} -> {dest}")


if __name__ == "__main__":
    app()
