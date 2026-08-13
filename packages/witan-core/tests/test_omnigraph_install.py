"""Tests for the shared omnigraph binary installer.

Moved here from the two servers' test_setup.py (where the installer was
duplicated). The per-server test_setup.py files keep only their own
registration-bundle tests.
"""

import hashlib
import io
import subprocess
import tarfile
from pathlib import Path

import pytest
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


def _serve(monkeypatch, blob: bytes) -> None:
    """Stub the download AND pin the digest of exactly what it will return.

    The installer verifies the tarball against ``_OMNIGRAPH_ASSET_SHA256``
    before extracting, so a fixture that stubbed only the transport is now
    refused — correctly, since that is the whole point of the pin. Registering
    the fake's own digest keeps these tests going THROUGH the verification path
    rather than around it; `test_a_tampered_download_is_refused` covers the
    mismatch case.
    """
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeResponse(blob))
    monkeypatch.setitem(
        oi._OMNIGRAPH_ASSET_SHA256,
        "omnigraph-linux-x86_64.tar.gz",
        hashlib.sha256(blob).hexdigest(),
    )


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
    _serve(monkeypatch, _fake_release_tarball(b"#!/bin/sh\necho fake"))

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
    _serve(monkeypatch, _fake_release_tarball(b"#!/bin/sh\necho fake"))

    oi.install_omnigraph(dry_run=False)

    assert _dest(tmp_path).read_bytes() == b"#!/bin/sh\necho fake"


#: The version an upgrade is coming *from*. Must differ from the current pin,
#: or `_download_omnigraph` short-circuits on the already-at-this-version skip
#: and never reaches the preservation step under test.
_OLD = "0.7.2"


def _upgrade_from(tmp_path, monkeypatch, version: str = _OLD) -> Path:
    """Run an install over an existing binary reporting ``version``."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(oi.platform, "system", lambda: "Linux")
    monkeypatch.setattr(oi.platform, "machine", lambda: "x86_64")
    _write_fake_binary(_dest(tmp_path), version)
    _serve(monkeypatch, _fake_release_tarball(b"#!/bin/sh\necho new"))
    oi.install_omnigraph(dry_run=False)
    return _dest(tmp_path)


def test_upgrade_sets_the_outgoing_binary_aside(tmp_path, monkeypatch):
    """The upgrade must not destroy the only binary that can export a store the
    new one refuses — that is the whole recovery path for a format bump."""
    dest = _upgrade_from(tmp_path, monkeypatch)

    kept = dest.with_name(f"omnigraph-{_OLD}")
    assert dest.read_bytes() == b"#!/bin/sh\necho new"
    assert kept.read_text() == f"#!/bin/sh\necho 'omnigraph {_OLD}'\n"
    assert kept.stat().st_mode & 0o111  # still runnable, or it can't export


def test_preserved_binaries_finds_what_the_upgrade_set_aside(tmp_path, monkeypatch):
    dest = _upgrade_from(tmp_path, monkeypatch)

    assert oi.preserved_binaries(dest) == [dest.with_name(f"omnigraph-{_OLD}")]


def test_every_previous_version_is_kept(tmp_path, monkeypatch):
    """NOTHING is pruned. A machine holds many stores at once — witan-code
    keeps one `<slug>.omni` per repository, migrated only when that repo is
    next opened — so a repo left untouched across two upgrades needs a binary
    older than the newest set-aside one. Sweeping to the newest deletes the
    only thing that could ever export it."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for older in ("0.5.0", "0.6.0"):
        _write_fake_binary(_dest(tmp_path).with_name(f"omnigraph-{older}"), older)

    dest = _upgrade_from(tmp_path, monkeypatch)

    survivors = sorted(p.name for p in dest.parent.glob("omnigraph-*"))
    assert survivors == ["omnigraph-0.5.0", "omnigraph-0.6.0", f"omnigraph-{_OLD}"]


def test_preserved_binaries_are_ordered_newest_first(tmp_path, monkeypatch):
    """Preference order for the migration's probe, and sorted by parsed version
    rather than filename so 0.10.0 outranks 0.9.0 instead of sorting under it."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for version in ("0.6.0", "0.10.0", "0.9.0"):
        _write_fake_binary(_dest(tmp_path).with_name(f"omnigraph-{version}"), version)

    dest = _dest(tmp_path)

    assert [p.name for p in oi.preserved_binaries(dest)] == [
        "omnigraph-0.10.0",
        "omnigraph-0.9.0",
        "omnigraph-0.6.0",
    ]


def test_a_binary_this_module_did_not_write_is_not_offered(tmp_path, monkeypatch):
    """`omnigraph-dev` is a user's own build sitting in the same directory. It
    is left alone, and it is not returned as a migration candidate either —
    `_PRESERVED_RE` matches only the exact `omnigraph-<semver>` names written
    here."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mine = _dest(tmp_path).with_name("omnigraph-dev")
    _write_fake_binary(mine, "9.9.9")

    dest = _upgrade_from(tmp_path, monkeypatch)

    assert mine.exists()
    assert oi.preserved_binaries(dest) == [dest.with_name(f"omnigraph-{_OLD}")]


def test_first_install_preserves_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(oi.platform, "system", lambda: "Linux")
    monkeypatch.setattr(oi.platform, "machine", lambda: "x86_64")
    _serve(monkeypatch, _fake_release_tarball(b"#!/bin/sh\necho new"))

    oi.install_omnigraph(dry_run=False)

    dest = _dest(tmp_path)
    assert list(dest.parent.glob("omnigraph-*")) == []
    assert oi.preserved_binaries(dest) == []


def test_preserve_failure_does_not_abort_the_upgrade(tmp_path, monkeypatch):
    """Setting the old binary aside is best-effort. Failing it leaves the user
    where they were before the feature existed; failing the install does not."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def _no_copy(*a, **k):
        raise OSError("no space left on device")

    monkeypatch.setattr(oi.shutil, "copy2", _no_copy)

    dest = _upgrade_from(tmp_path, monkeypatch)

    assert dest.read_bytes() == b"#!/bin/sh\necho new"
    assert oi.preserved_binaries(dest) == []


def _fake_version_binary(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def test_reported_internal_schema_reads_the_version_line(tmp_path):
    binary = _fake_version_binary(
        tmp_path / "og", "echo 'omnigraph 0.9.0'\necho 'internal-schema 6'"
    )

    assert oi.reported_internal_schema(binary) == 6


def test_reported_internal_schema_raises_when_the_line_is_gone(tmp_path):
    """Never a sentinel. Every caller compares the result against a declared
    format, and a comparison against "unknown" that quietly passes is the
    failure this whole mechanism exists to prevent."""
    binary = _fake_version_binary(tmp_path / "og", "echo 'omnigraph 1.0.0'")

    with pytest.raises(RuntimeError, match="no internal-schema line"):
        oi.reported_internal_schema(binary)


def test_reported_internal_schema_raises_on_a_failing_binary(tmp_path):
    binary = _fake_version_binary(tmp_path / "og", "echo boom >&2\nexit 3")

    with pytest.raises(RuntimeError, match="failed"):
        oi.reported_internal_schema(binary)


def test_reported_internal_schema_raises_when_the_binary_is_missing(tmp_path):
    with pytest.raises(RuntimeError, match="could not run"):
        oi.reported_internal_schema(tmp_path / "does-not-exist")


def test_the_declared_internal_schema_is_a_plausible_version():
    """Guards a typo in the declaration itself — it is a hand-edited number
    that gates a rebuild-everything decision, and `bin/check_omnigraph_format.py`
    trusts it completely."""
    assert isinstance(oi._OMNIGRAPH_INTERNAL_SCHEMA, int)
    assert oi._OMNIGRAPH_INTERNAL_SCHEMA > 0


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


def test_a_tampered_download_is_refused(tmp_path, monkeypatch, capsys):
    """★ The pin's whole purpose: bytes that are not the build this repo was
    tested against must not reach a developer's PATH.

    On a moving tag (`edge`) this is also the only thing tying the installed
    binary to a specific upstream commit — equal version and tag strings can
    still resolve to different builds. Refuse, do not warn: a warning during
    `witan setup` is read as noise, and the binary is already installed by then.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(oi.platform, "system", lambda: "Linux")
    monkeypatch.setattr(oi.platform, "machine", lambda: "x86_64")
    blob = _fake_release_tarball(b"#!/bin/sh\necho tampered")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeResponse(blob))
    monkeypatch.setitem(
        oi._OMNIGRAPH_ASSET_SHA256, "omnigraph-linux-x86_64.tar.gz", "00" * 32
    )

    oi.install_omnigraph(dry_run=False)

    assert not _dest(tmp_path).exists(), "a mismatched download must not install"
    out = capsys.readouterr().out
    assert "checksum mismatch" in out
    assert "has moved" in out  # names the likely cause on a moving tag


def test_an_asset_with_no_pinned_digest_is_refused(tmp_path, monkeypatch, capsys):
    """A platform added to `_OMNIGRAPH_ASSETS` and forgotten in the digest map
    must fail closed. The inverse — installing unverified because the pin is
    absent — is how the verification quietly stops applying."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(oi.platform, "system", lambda: "Linux")
    monkeypatch.setattr(oi.platform, "machine", lambda: "x86_64")
    blob = _fake_release_tarball(b"#!/bin/sh\necho fake")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeResponse(blob))
    monkeypatch.delitem(oi._OMNIGRAPH_ASSET_SHA256, "omnigraph-linux-x86_64.tar.gz")

    oi.install_omnigraph(dry_run=False)

    assert not _dest(tmp_path).exists()
    assert "no pinned checksum" in capsys.readouterr().out
