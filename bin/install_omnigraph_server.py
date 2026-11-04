#!/usr/bin/env -S uv run --quiet --package witan-core python
"""Install ``omnigraph-server`` from the pinned, checksummed omnigraph release.

``install_omnigraph`` extracts only the CLI, because that is all ``witan
setup`` needs. The served tests (``testsupport/omnigraph_server.py``, used by
witan's group-commit tests and witan-code's branch-view tests) start a real
``omnigraph-server`` from the same tarball, and without it they skip, so CI
never ran the default write path against a real server.

``--release previous`` fetches the release on the other contract in
``SUPPORTED_HTTP_API_CONTRACTS`` instead, with its own CLI beside it (the
served tests apply their cluster with the server's sibling ``omnigraph``).
Point ``OMNIGRAPH_PREVIOUS_SERVER_BIN`` at it and the served tests run against
both servers.
"""

import hashlib
import platform
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Literal

import cyclopts

from witan_core.omnigraph_http import SUPPORTED_HTTP_API_CONTRACTS
from witan_core.omnigraph_install import (
    _OMNIGRAPH_ASSET_SHA256,
    _OMNIGRAPH_ASSETS,
    _OMNIGRAPH_RELEASE_TAG,
    default_install_path,
)

SERVER_BINARY = "omnigraph-server"
CLI_BINARY = "omnigraph"
DOWNLOAD_TIMEOUT_SECONDS = 120

#: The last release on the other supported contract, and its digests: the
#: v0.13.0 values `_OMNIGRAPH_ASSET_SHA256` held before the 0.14 pin, which
#: the linux-x86_64 tarball still matched on 2026-11-04. Moves, or goes, when
#: `SUPPORTED_HTTP_API_CONTRACTS` does.
PREVIOUS_RELEASE_TAG = "v0.13.0"
PREVIOUS_RELEASE_CONTRACT = "0.13"
PREVIOUS_ASSET_SHA256 = {
    "omnigraph-linux-x86_64.tar.gz": (
        "0123daa64f0f2db9860284200bd71c79a22a6ad2c6f5501b6fca24cac86a7a30"
    ),
    "omnigraph-linux-arm64.tar.gz": (
        "e61e7379131a05056bfeace206c8c5bcdf185d169d1ce86bc192d52f13693f96"
    ),
    "omnigraph-macos-arm64.tar.gz": (
        "0a1c7f013e18465254fb925dc177f3089bf6e0cfe4a0ecfc9908a180ab84cc72"
    ),
}
assert PREVIOUS_RELEASE_CONTRACT in SUPPORTED_HTTP_API_CONTRACTS[1:], (
    f"{PREVIOUS_RELEASE_TAG} speaks {PREVIOUS_RELEASE_CONTRACT}, which the client "
    "no longer negotiates; move PREVIOUS_RELEASE_TAG with "
    "SUPPORTED_HTTP_API_CONTRACTS"
)

app = cyclopts.App(help=__doc__)


@app.default
def main(
    dest: Path | None = None, release: Literal["pinned", "previous"] = "pinned"
) -> None:
    """Fetch a release, verify it, and extract ``omnigraph-server``.

    :param dest: where to put the binary. Defaults to beside the CLI
        (``~/.local/bin/omnigraph-server``) for the pinned release. The
        previous release has no default, because a second ``omnigraph`` on
        PATH would shadow the pinned one.
    :param release: ``pinned`` for the release witan-core pins, ``previous``
        for :data:`PREVIOUS_RELEASE_TAG`, whose CLI is extracted beside it.
    """
    if release == "pinned":
        tag, digests, binaries = _OMNIGRAPH_RELEASE_TAG, _OMNIGRAPH_ASSET_SHA256, ()
        dest = dest or default_install_path().with_name(SERVER_BINARY)
    else:
        if dest is None:
            raise SystemExit("--release previous needs --dest")
        tag, digests, binaries = (
            PREVIOUS_RELEASE_TAG,
            PREVIOUS_ASSET_SHA256,
            (CLI_BINARY,),
        )
    asset = _OMNIGRAPH_ASSETS[(platform.system().lower(), platform.machine().lower())]
    url = f"https://github.com/ModernRelay/omnigraph/releases/download/{tag}/{asset}"
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / asset
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as resp:
            archive.write_bytes(resp.read())
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        expected = digests[asset]
        if actual != expected:
            msg = f"{asset}: expected sha256 {expected}, got {actual}"
            raise SystemExit(msg)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as tf:
            for name, target in (
                (SERVER_BINARY, dest),
                *((binary, dest.with_name(binary)) for binary in binaries),
            ):
                (member,) = (
                    m
                    for m in tf.getmembers()
                    if m.name.split("/")[-1] == name and m.isfile()
                )
                extracted = tf.extractfile(member)
                assert extracted is not None
                target.write_bytes(extracted.read())
                target.chmod(0o755)
                print(f"{name} {tag} -> {target}")


if __name__ == "__main__":
    app()
