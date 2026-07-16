"""Tests for the shared omnigraph binary installer.

Moved here from the two servers' test_setup.py (where the installer was
duplicated). The per-server test_setup.py files keep only their own
registration-bundle tests.
"""

import io
import subprocess
import tarfile
from pathlib import Path

from witan_core import omnigraph_install as oi


def _fake_release_tarball(binary_content: bytes) -> bytes:
    """A minimal in-memory ``.tar.gz`` shaped like a real omnigraph release
    asset: the binary nested one directory down, matching the
    ``member.name.split("/")[-1] == "omnigraph"`` match in ``_download_omnigraph``."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = io.BytesIO(binary_content)
        info = tarfile.TarInfo(name="omnigraph-linux-x86_64/omnigraph")
        info.size = len(binary_content)
        tf.addfile(info, data)
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._data


def _dest(tmp_path: Path) -> Path:
    return tmp_path / ".local" / "bin" / "omnigraph"


def _write_fake_binary(dest: Path, version: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(f"#!/bin/sh\necho 'omnigraph {version}'\n")
    dest.chmod(0o755)


def test_install_omnigraph_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(oi.platform, "system", lambda: "Linux")
    monkeypatch.setattr(oi.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry-run hit network")),
    )

    oi.install_omnigraph(dry_run=True)

    assert not _dest(tmp_path).exists()


def test_install_omnigraph_unsupported_platform_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(oi.platform, "system", lambda: "plan9")
    monkeypatch.setattr(oi.platform, "machine", lambda: "mips")

    oi.install_omnigraph(dry_run=False)

    assert not _dest(tmp_path).exists()


def test_install_omnigraph_downloads_and_extracts(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(oi.platform, "system", lambda: "Linux")
    monkeypatch.setattr(oi.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: _FakeResponse(_fake_release_tarball(b"#!/bin/sh\necho fake")),
    )

    oi.install_omnigraph(dry_run=False)

    dest = _dest(tmp_path)
    assert dest.read_bytes() == b"#!/bin/sh\necho fake"
    assert dest.stat().st_mode & 0o111  # executable


def test_install_omnigraph_skips_when_version_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_fake_binary(_dest(tmp_path), oi._OMNIGRAPH_VERSION)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("hit network")),
    )

    oi.install_omnigraph(dry_run=False)

    assert (
        _dest(tmp_path).read_text()
        == f"#!/bin/sh\necho 'omnigraph {oi._OMNIGRAPH_VERSION}'\n"
    )


def test_install_omnigraph_redownloads_when_version_differs(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(oi.platform, "system", lambda: "Linux")
    monkeypatch.setattr(oi.platform, "machine", lambda: "x86_64")
    _write_fake_binary(_dest(tmp_path), "0.1.0")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: _FakeResponse(_fake_release_tarball(b"#!/bin/sh\necho fake")),
    )

    oi.install_omnigraph(dry_run=False)

    assert _dest(tmp_path).read_bytes() == b"#!/bin/sh\necho fake"


def test_installed_version_none_on_timeout(tmp_path, monkeypatch):
    dest = _dest(tmp_path)
    _write_fake_binary(dest, oi._OMNIGRAPH_VERSION)
    monkeypatch.setattr(
        oi.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd=str(dest), timeout=10)
        ),
    )

    assert oi._installed_version(dest) is None


def test_installed_version_none_on_nonzero_exit(tmp_path):
    dest = _dest(tmp_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(f"#!/bin/sh\necho 'omnigraph {oi._OMNIGRAPH_VERSION}'\nexit 1\n")
    dest.chmod(0o755)

    assert oi._installed_version(dest) is None
