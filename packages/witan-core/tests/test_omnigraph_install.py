"""Tests for the shared omnigraph binary installer.

Moved here from the two servers' test_setup.py (where the installer was
duplicated). The per-server test_setup.py files keep only their own
registration-bundle tests.
"""

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Self

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

    def __enter__(self) -> Self:
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


def _newer_than_the_pin() -> str:
    major, minor, _ = oi._version_key(oi._OMNIGRAPH_VERSION)
    return f"{major}.{minor + 1}.0"


def _install_over_a_newer_binary(tmp_path, monkeypatch, *, strict: bool) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(oi.platform, "system", lambda: "Linux")
    monkeypatch.setattr(oi.platform, "machine", lambda: "x86_64")
    _write_fake_binary(_dest(tmp_path), _newer_than_the_pin())
    # A download that would succeed, so leaving the binary alone is the
    # installer's decision and not a failed fetch.
    _serve(monkeypatch, _fake_release_tarball(b"#!/bin/sh\necho older"))
    oi.install_omnigraph(dry_run=False, strict=strict)
    return _dest(tmp_path)


def test_a_newer_binary_is_not_replaced_by_the_older_pin(tmp_path, monkeypatch, capsys):
    """A newer binary may already have stamped local stores with a format the
    pin refuses (0.14 writes 16, which 0.13 won't open), so `witan setup`,
    which installs non-strict, leaves it alone and says so."""
    dest = _install_over_a_newer_binary(tmp_path, monkeypatch, strict=False)

    assert dest.read_text() == f"#!/bin/sh\necho 'omnigraph {_newer_than_the_pin()}'\n"
    assert oi.preserved_binaries(dest) == []
    # Rich wraps at the console width, so compare with the whitespace folded.
    said = " ".join(capsys.readouterr().out.split())
    assert f"newer than this witan's pin v{oi._OMNIGRAPH_VERSION}" in said
    assert "re-run to install" in said


def test_a_newer_binary_fails_a_strict_install(tmp_path, monkeypatch):
    """The CI steps install strict: they asked for the pin and didn't get it."""
    with pytest.raises(oi.OmnigraphInstallFailed, match="newer than the pinned"):
        _install_over_a_newer_binary(tmp_path, monkeypatch, strict=True)

    assert _dest(tmp_path).read_text() == (
        f"#!/bin/sh\necho 'omnigraph {_newer_than_the_pin()}'\n"
    )


def test_an_older_binary_with_a_longer_minor_is_still_upgraded(tmp_path, monkeypatch):
    """Ordered as numbers, not strings: "0.9.0" sorts above "0.14.0" as text."""
    monkeypatch.setattr(oi, "_OMNIGRAPH_VERSION", "0.14.0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(oi.platform, "system", lambda: "Linux")
    monkeypatch.setattr(oi.platform, "machine", lambda: "x86_64")
    _write_fake_binary(_dest(tmp_path), "0.9.0")
    _serve(monkeypatch, _fake_release_tarball(b"#!/bin/sh\necho fake"))

    oi.install_omnigraph(dry_run=False)

    assert _dest(tmp_path).read_bytes() == b"#!/bin/sh\necho fake"


#: The version an upgrade is coming *from*. Must be older than the current
#: pin: an equal one short-circuits on the already-at-this-version skip and a
#: newer one is refused, so neither reaches the preservation step under test.
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


#: `omnigraph version --json` from the real v0.14.0 linux-x86_64 release
#: (digest in `_OMNIGRAPH_ASSET_SHA256`'s history). The fields and values are as
#: the binary printed them; biome re-indents the file.
_VERSION_0_14_0 = Path(__file__).parent / "fixtures" / "omnigraph-version-0.14.0.json"
#: The same from the real v0.15.0 linux-x86_64 release (the pinned digest),
#: which adds a `studio` protocol and moves neither the format nor the HTTP
#: contract.
_VERSION_0_15_0 = Path(__file__).parent / "fixtures" / "omnigraph-version-0.15.0.json"


def _json_version_binary(path: Path, document: str) -> Path:
    """A fake binary answering `version --json` with ``document``, and refusing
    anything else the way clap does."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = path.with_suffix(".json")
    doc.write_text(document)
    path.write_text(
        "#!/bin/sh\n"
        f'if [ "$1 $2" = "version --json" ]; then cat \'{doc}\'; exit 0; fi\n'
        "echo 'error: unexpected argument' >&2\nexit 2\n"
    )
    path.chmod(0o755)
    return path


def test_reported_build_reads_the_0_14_document(tmp_path):
    binary = _json_version_binary(tmp_path / "og", _VERSION_0_14_0.read_text())

    assert oi.reported_build(binary) == oi.OmnigraphBuild(
        version="0.14.0",
        commit="60bfd19db780fdff3a5178f959c1204143420114",
        http_contract="0.14",
        writes=16,
        serves=(14, 16),
    )


def test_reported_build_reads_the_0_15_document(tmp_path):
    binary = _json_version_binary(tmp_path / "og", _VERSION_0_15_0.read_text())

    assert oi.reported_build(binary) == oi.OmnigraphBuild(
        version="0.15.0",
        commit="373834b4c1c4b29cf0ddd64bee9565f98c60cc26",
        http_contract="0.14",
        writes=16,
        serves=(14, 16),
    )


def test_reported_build_reads_a_null_commit(tmp_path):
    """Upstream reports `commit: null` for any build that isn't a release
    archive, e.g. one built from source."""
    doc = json.loads(_VERSION_0_14_0.read_text())
    doc["commit"] = None
    binary = _json_version_binary(tmp_path / "og", json.dumps(doc))

    assert oi.reported_build(binary).commit is None


def test_reported_build_refuses_a_pre_0_14_binary(tmp_path):
    """0.13 exits 2 on `--json`. No fallback to its text output: every caller
    is checking the pinned binary, so this is a stale install."""
    binary = _fake_version_binary(
        tmp_path / "og",
        "echo \"error: unexpected argument '--json' found\" >&2\nexit 2",
    )

    with pytest.raises(RuntimeError, match="added `version --json` in 0.14"):
        oi.reported_build(binary)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc.pop("internal_schema"),
        lambda doc: doc["internal_schema"].pop("writes"),
        lambda doc: doc["internal_schema"].update(serves=[16]),
        lambda doc: doc.pop("http_contract"),
    ],
    ids=["no-internal-schema", "no-writes", "serves-not-a-range", "no-contract"],
)
def test_reported_build_raises_on_a_document_it_cannot_read(tmp_path, mutate):
    """Never a sentinel. Every caller compares the result against a declared
    format, and a comparison against "unknown" that quietly passes is the
    failure this whole mechanism exists to prevent."""
    doc = json.loads(_VERSION_0_14_0.read_text())
    mutate(doc)
    binary = _json_version_binary(tmp_path / "og", json.dumps(doc))

    with pytest.raises(RuntimeError, match="can't read"):
        oi.reported_build(binary)


def test_reported_build_raises_on_output_that_is_not_json(tmp_path):
    binary = _json_version_binary(tmp_path / "og", "omnigraph 0.14.0\n")

    with pytest.raises(RuntimeError, match="can't read"):
        oi.reported_build(binary)


def test_reported_build_raises_when_the_binary_is_missing(tmp_path):
    with pytest.raises(RuntimeError, match="could not run"):
        oi.reported_build(tmp_path / "does-not-exist")


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

    with pytest.raises(oi.OmnigraphInstallFailed) as raised:
        oi.install_omnigraph(dry_run=False)

    assert not _dest(tmp_path).exists(), "a mismatched download must not install"
    out = capsys.readouterr().out
    assert "checksum mismatch" in out
    assert "has moved" in out  # names the likely cause on a moving tag
    # The exception carries the same facts, unmarked-up: it is what a CI step's
    # traceback shows, and Rich markup there would be swallowed by any console
    # that rendered it.
    message = str(raised.value)
    assert "checksum mismatch" in message
    assert "has moved" in message
    assert "[red]" not in message


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

    with pytest.raises(oi.OmnigraphInstallFailed, match="no pinned checksum"):
        oi.install_omnigraph(dry_run=False)

    assert not _dest(tmp_path).exists()
    assert "no pinned checksum" in capsys.readouterr().out


# ── a refusal has to reach the exit code ───────────────────────────
#
# Every path above printed its reason and returned, so a workflow step running
# `install_omnigraph(dry_run=False)` exited 0 with the refusal buried in its
# log. It resurfaced ten tests later as `omnigraph binary not found`, which
# reads as a broken test environment rather than as the digest check working.


def _serve_mismatched(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(oi.platform, "system", lambda: "Linux")
    monkeypatch.setattr(oi.platform, "machine", lambda: "x86_64")
    blob = _fake_release_tarball(b"#!/bin/sh\necho tampered")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeResponse(blob))
    monkeypatch.setitem(
        oi._OMNIGRAPH_ASSET_SHA256, "omnigraph-linux-x86_64.tar.gz", "00" * 32
    )


def test_the_interactive_path_can_opt_out_and_keep_going(tmp_path, monkeypatch, capsys):
    """`witan setup` asks for several unrelated things in one run. A refused
    binary must not cost the user config.toml and their agent bundles, which
    are useful on their own — so that caller passes strict=False and still
    gets the printed reason."""
    _serve_mismatched(tmp_path, monkeypatch)

    oi.install_omnigraph(dry_run=False, strict=False)

    assert not _dest(tmp_path).exists()
    assert "checksum mismatch" in capsys.readouterr().out


def test_a_failed_download_is_an_error_too(tmp_path, monkeypatch):
    """Not only the digest check. A transient fetch failure swallowed here
    surfaces the same confusing way — as a missing binary, much later."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(oi.platform, "system", lambda: "Linux")
    monkeypatch.setattr(oi.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(OSError("connection reset")),
    )

    with pytest.raises(oi.OmnigraphInstallFailed, match="connection reset"):
        oi.install_omnigraph(dry_run=False)


def test_an_archive_without_the_binary_is_an_error(tmp_path, monkeypatch):
    """The release asset layout changing is a real failure, not a skip."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(oi.platform, "system", lambda: "Linux")
    monkeypatch.setattr(oi.platform, "machine", lambda: "x86_64")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        payload = io.BytesIO(b"not the binary")
        info = tarfile.TarInfo(name="omnigraph-linux-x86_64/README")
        info.size = 14
        tf.addfile(info, payload)
    _serve(monkeypatch, buf.getvalue())

    with pytest.raises(oi.OmnigraphInstallFailed, match="no omnigraph binary inside"):
        oi.install_omnigraph(dry_run=False)


def test_an_unsupported_platform_is_still_a_skip(tmp_path, monkeypatch):
    """NOT every non-install is a failure. This installer builds for a fixed
    set of platforms, and witan works fine with an omnigraph put on PATH by
    other means — raising here would break those users for no gain."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(oi.platform, "system", lambda: "plan9")
    monkeypatch.setattr(oi.platform, "machine", lambda: "mips")

    oi.install_omnigraph(dry_run=False)

    assert not _dest(tmp_path).exists()


def test_an_already_current_binary_is_still_a_skip(tmp_path, monkeypatch):
    """The converged state re-running is supposed to reach, not a failure."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_fake_binary(_dest(tmp_path), oi._OMNIGRAPH_VERSION)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("hit network")),
    )

    oi.install_omnigraph(dry_run=False)


def test_the_specific_reason_survives_the_catch_all(tmp_path, monkeypatch):
    """The outer `except Exception` wraps anything unexpected as a generic
    install failure. A refusal must pass through it intact, or the message that
    names the moved tag is replaced by one that names nothing."""
    _serve_mismatched(tmp_path, monkeypatch)

    with pytest.raises(oi.OmnigraphInstallFailed) as raised:
        oi.install_omnigraph(dry_run=False)

    assert "checksum mismatch" in str(raised.value)
    assert "install failed" not in str(raised.value)
