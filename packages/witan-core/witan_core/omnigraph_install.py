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

import hashlib
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

#: ★ TEMPORARILY ON `edge` (0.10.0) FOR A RE-TEST — NOT A DECISION TO ADOPT IT.
#: 0.10.0 was reverted on 2026-08-14 for halving the write ceiling
#: (agent-kit#233). Three witan-side confounds have since been fixed and the
#: measurement is worth repeating; see
#: tk-omnigraph-0-10-0-edge-halved-the-write-ceiling-r-7ba7c2 for the
#: hypothesis and the revert procedure. If you are reading this after the
#: experiment concluded, it should already be back on 0.9.0/v0.9.0 — if it is
#: not, that is the bug.
_OMNIGRAPH_VERSION = "0.10.0"

#: WHICH UPSTREAM TAG THE BINARY IS FETCHED FROM. Normally ``v`` + the version
#: above; ``edge`` selects the rolling build of upstream ``main``, which
#: ``release-edge.yml`` force-updates and re-publishes on every push there.
#:
#: Separate from ``_OMNIGRAPH_VERSION`` because on a moving tag the two genuinely
#: differ: ``edge`` currently ships a binary that reports ``0.10.0``, and there is
#: no ``v0.10.0`` release to download. Collapsing them into one string would
#: either break the URL or break the "already installed, skipping" check, which
#: compares against what ``omnigraph --version`` actually prints.
#:
#: ★ A MOVING TAG WEAKENS THAT SKIP CHECK, and the caveat is the price of using
#: one: two different `edge` builds both report ``0.10.0``, so a machine that
#: installed yesterday's will not re-download today's.
#:
#: There is no flag or environment override for this — the tag is a property of
#: the repo, not of a run, precisely because all three tiers must agree on it
#: (``just check-omnigraph-pins``). To be certain which build you are on:
#: delete the binary and re-run ``witan setup``, which re-downloads and verifies
#: against the digest pinned below. To move OFF the moving tag, edit this
#: constant to ``v<version>`` and refresh those digests in the same commit.
#:
#: Renovate manages the VERSION line only (see renovate.json). While this is
#: ``edge`` a bump is not meaningful, so pin a real ``v<version>`` before
#: treating dependency updates here as authoritative.
_OMNIGRAPH_RELEASE_TAG = "edge"

#: The on-disk storage format ``_OMNIGRAPH_VERSION`` is expected to read, as
#: reported by ``omnigraph version``'s ``internal-schema`` line. 0.8.x reads 4;
#: 0.9.x reads 6.
#:
#: THIS IS A DECLARATION, NOT A CACHE. Renovate bumps the version pin above and
#: cannot know about this line, so a release that moves the storage format
#: leaves the two disagreeing — which is exactly the signal
#: ``bin/check_omnigraph_format.py`` turns into a failing check. Editing this
#: number is how a human says "yes, I know this rebuilds every graph, and the
#: migration is planned".
#:
#: Do not update it to make CI green. Updating it is the last step of a format
#: migration, not the first: every local store and every deployed graph written
#: under the old number has to be rebuilt, and a 0.8.x binary refuses a 0.9.x
#: graph in both directions, so there is no gradual path and no downgrade.
_OMNIGRAPH_INTERNAL_SCHEMA = 6

_OMNIGRAPH_ASSETS: dict[tuple[str, str], str] = {
    ("linux", "x86_64"): "omnigraph-linux-x86_64.tar.gz",
    ("darwin", "arm64"): "omnigraph-macos-arm64.tar.gz",
}

#: SHA-256 of each asset, pinned in-repo. Keyed by asset NAME rather than by
#: platform so the two Dockerfiles — which select by ``TARGETARCH``, not by
#: ``platform.system()`` — can carry the identical values and
#: ``just check-omnigraph-pins`` can compare them across all three tiers.
#:
#: ★ THIS IS WHAT MAKES A MOVING TAG REPRODUCIBLE, AND IT IS NOT OPTIONAL WHILE
#: WE ARE ON ONE. ``edge`` is force-updated on every push to upstream main, so
#: the tag alone guarantees nothing: the installer and the two image builds can
#: each resolve it to a different commit and every version/tag check still
#: passes. A load-test result measured that way cannot be attributed to a build.
#: Downloading the published ``.sha256`` alongside the tarball does not fix that
#: either — it only attests to whichever build was current at download time.
#:
#: So the digest is recorded here, and a mismatch is a hard failure. When
#: upstream pushes, the next fetch FAILS LOUDLY rather than silently installing
#: a different binary; refreshing these values is then a deliberate act that
#: says "I am moving to a new build", exactly as editing
#: ``_OMNIGRAPH_INTERNAL_SCHEMA`` says "I know this rebuilds every graph".
#:
#: Refresh with, for each asset — note the published digest file drops the
#: `.tar.gz`, so it is `omnigraph-linux-x86_64.sha256`, not
#: `omnigraph-linux-x86_64.tar.gz.sha256` (that spelling 404s):
#:     curl -fsSL https://github.com/ModernRelay/omnigraph/releases/download/\
#: <tag>/<asset-without-.tar.gz>.sha256
#: and confirm it against the tarball you actually downloaded (`sha256sum`) in
#: the same sitting — on a moving tag the two assets can be republished a
#: minute apart, and a digest read across that gap describes neither build.
#: ★ THESE ARE THE `edge` BUILD OF 2026-08-21T00:11Z (through 62a9c3fe6b),
#: NOT v0.9.0's. Refreshed from the 2026-08-20T17:18Z triple (bee47cd465)
#: after CI failed the checksum check on 2026-08-21 (agent-kit#272). Six
#: commits landed in between, all storage-layer typed-failure work
#: (docs/rfcs/0038-typed-storage-failures.md): `OmniError::Lance(String)`
#: became `OmniError::Storage(StorageFailure)`, closed over a shared
#: `omnigraph-storage` crate. Checked the diff for the two things that would
#: matter to witan — the `_RETRYABLE`/`_NEEDS_REPAIR`/`_PRECONDITION_FAILED`
#: substrings in omnigraph.py, and the `"storage: "` prose prefix witan's
#: classifier keys on — and found neither renamed: the refactor keeps
#: `STORAGE_MESSAGE_PREFIX = "storage: "` and its own new tests assert the
#: same `storage: <message>` rendering the old `Lance` variant produced. No
#: vocabulary or JSON-output change here, unlike the 69d292ce80/ecf1d6aedd
#: rename two refreshes back.
#:
#: ★ AND `edge` MOVED THREE TIMES WHILE THE PRIOR TRIPLE WAS BEING WRITTEN —
#: see the git history of this comment for that episode. That is the cost of
#: the moving tag, not a mishap: upstream merges several times a day and each
#: push republishes `edge`, so a digest here can be stale before CI runs.
#: Expect to refresh this on a red witan-code job rather than on a schedule,
#: and prefer a real `v<version>` tag the moment 0.10.x has one (there is no
#: v0.10.0 release yet, which is the only reason this is still on `edge`).
#:
#: The digests below were taken by downloading all three tarballs and hashing
#: them locally, then cross-checking each against the release's published
#: `.sha256` in the same sitting. Still the 0.10.0 re-test
#: (tk-omnigraph-0-10-0-edge-halved-the-write-ceiling-r-7ba7c2); version still
#: reports 0.10.0 and internal-schema still 6, both read off this binary.
#: Reverting the experiment means restoring the v0.9.0 triple, which was:
#:     linux-x86_64  507a36f385bea073e7f284fe476befbb4cd788b32bfa85d6f4cd5e943b663197
#:     linux-arm64   6742a7fcf2761cb5841a38990c38383d7a884da2c65e3e7cc884afbbf2b2d881
#:     macos-arm64   69f78c93e661e8ea2b92deafe6330650a0921a003c2099b75b226482a90dc03e
_OMNIGRAPH_ASSET_SHA256: dict[str, str] = {
    "omnigraph-linux-x86_64.tar.gz": (
        "68099e33941cc5c252f36d4c2a26f1dfff6b28e4eb627ba1f7bf098856d34349"
    ),
    "omnigraph-linux-arm64.tar.gz": (
        "d0e42176625584370a26c8a6ca43bc24447ea822483c46d971807475a5a5782a"
    ),
    "omnigraph-macos-arm64.tar.gz": (
        "f30f37f0ad8084ed5b26ce5163ebac04554c89bec4259aa1763826bc1efe215f"
    ),
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


def reported_internal_schema(binary: str | Path = "omnigraph") -> int:
    """The on-disk storage format ``binary`` reads, per ``omnigraph version``.

    The number that decides whether an upgrade is a rebuild-everything event.
    Read from the binary rather than inferred from its release number, because
    the mapping is upstream's to change and has no published table.

    Raises ``RuntimeError`` rather than returning a sentinel: every caller is
    asking in order to compare against a declared value, and a comparison
    against "unknown" that quietly passes is the failure mode this whole
    mechanism exists to prevent.
    """
    try:
        result = subprocess.run(
            [str(binary), "version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"could not run `{binary} version`: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"`{binary} version` failed ({result.returncode}):\n{result.stderr}"
        )
    for line in (result.stdout + result.stderr).splitlines():
        if line.strip().startswith("internal-schema"):
            return int(line.split()[-1])
    raise RuntimeError(
        f"`{binary} version` reported no internal-schema line:\n{result.stdout}\n"
        "The storage-format checks depend on it; upstream may have renamed or "
        "dropped it."
    )


def preserved_binaries(dest: Path | None = None) -> list[Path]:
    """Every pre-upgrade binary this installer set aside, newest version first.

    Named ``omnigraph-<version>`` beside ``dest`` by :func:`_preserve_outgoing`.

    ALL OF THEM, NOT JUST THE NEWEST, and the caller is expected to try each in
    turn. There is no single "the previous binary", because there is no single
    store: witan keeps one memory graph, but witan-code keeps a separate
    ``<slug>.omni`` per repository (``witan_code.config.Config.code_dir``), and
    those are only ever migrated when someone next opens that repo. Cross two
    format versions while a repo sits untouched and its store is two releases
    behind — older than the newest set-aside binary, and readable only by one
    further back.

    Ordering is by parsed version rather than filename, so ``0.10.0`` sorts
    above ``0.9.0`` instead of below it.
    """
    target = dest or default_install_path()
    found: list[tuple[tuple[int, ...], Path]] = []
    for entry in target.parent.glob("omnigraph-*"):
        match = _PRESERVED_RE.match(entry.name)
        if match and entry.is_file() and os.access(entry, os.X_OK):
            parsed = tuple(int(part) for part in match.group(1).split("."))
            found.append((parsed, entry))
    return [path for _, path in sorted(found, reverse=True)]


class OmnigraphInstallFailed(RuntimeError):
    """The installer declined to put a binary in place, and said why.

    ★ RAISED SO THE STEP THAT ASKED FOR THE INSTALL IS THE STEP THAT FAILS.
    Every path below used to print its reason and return, so a workflow step
    running `install_omnigraph(dry_run=False)` exited 0 with the refusal buried
    in its log. The refusal then resurfaced ten tests later as `RuntimeError:
    omnigraph binary not found. Install via: witan-code setup` — which reads as
    a broken test environment rather than as a supply-chain check doing exactly
    its job, and cost real time to trace on 2026-08-20.

    A moved `edge` tag is the common cause and the one worth naming: the digest
    check catching it is the system working, and it should look like it.
    """


def install_omnigraph(dry_run: bool = False, *, strict: bool = True) -> None:
    """Fetch the pinned omnigraph release into ``~/.local/bin/``.

    Skips the download when a binary is already present and reports the
    pinned version via ``--version``, so re-running always converges on the
    current pin without refetching an already-correct binary.

    ``strict`` (the default) raises :class:`OmnigraphInstallFailed` when no
    binary ends up installed. Pass ``strict=False`` to keep the old
    print-and-return behaviour.

    ★ THE DEFAULT IS THE STRICT ONE ON PURPOSE. The callers that most need the
    failure are the seven workflow steps invoking this through `python -c`, and
    they cannot pass an argument without being edited — so the default has to
    be the loud one or they keep swallowing it. The two callers that legitimately
    want to continue are `witan setup` and `witan code setup`, which are
    interactive, ask for several unrelated things in one run, and would
    otherwise abort before writing config.toml and the agent bundles over a
    binary the user can install separately. Those two opt out explicitly.

    NOT AN ERROR EITHER WAY: an unsupported platform, and a binary already at
    the pinned version. Neither is a failure to install — the first is a
    platform this installer does not build for (witan works fine with an
    omnigraph put on PATH by other means), and the second is the converged
    state re-running is supposed to reach.
    """
    _download_omnigraph(default_install_path(), dry_run, strict=strict)


def _preserve_outgoing(dest: Path, version: str | None, console) -> None:
    """Set the outgoing binary aside as ``omnigraph-<version>`` beside ``dest``.

    Copied rather than moved: the copy runs *before* the atomic replace, and a
    move would leave the user with no working ``omnigraph`` at all in the
    window between the two, or permanently if the replace then failed.

    ★ EVERY PREVIOUS VERSION IS KEPT. Nothing is pruned here, and that is
    deliberate — an earlier revision of this function swept all but the newest,
    on the reasoning that a store has exactly one writer. True per store, and
    irrelevant: there are many stores. witan-code keeps one ``<slug>.omni`` per
    repository, each migrated only when someone next opens that repo. Upgrade
    across two format versions while a repo lies untouched and its store is two
    releases behind — so the sweep would delete the only binary able to export
    it, permanently, with no warning and no way back.

    The cost of not pruning is disk (these binaries are ~220 MB each) bounded
    by how many format versions a machine traverses, which is small. The cost
    of pruning is unrecoverable data. Retiring old copies is safe only once
    every store is known migrated, which this function cannot know and should
    not guess.

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


def _download_omnigraph(dest: Path, dry_run: bool, *, strict: bool = True) -> None:
    try:
        from rich.console import Console
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise RuntimeError(
            "the omnigraph installer needs `rich` for its progress output; "
            "install it via the witan-core[cli] extra (both servers already "
            "depend on rich, so this only bites a bare witan-core install)."
        ) from exc

    console = Console()

    def refuse(markup: str, plain: str) -> None:
        """Print the reason as before, then raise it unless the caller opted out.

        Two texts rather than one: the console gets Rich markup, and the
        exception must not — a `[red]` in an exception message is noise in a
        traceback and, worse, is swallowed whole by anything that renders it
        through Rich (`witan setup`'s own console does).
        """
        console.print(markup)
        if strict:
            raise OmnigraphInstallFailed(plain)

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
        f"/{_OMNIGRAPH_RELEASE_TAG}/{asset}"
    )
    console.print(
        f"  downloading omnigraph {_OMNIGRAPH_RELEASE_TAG} "
        f"(expected v{_OMNIGRAPH_VERSION}) …"
    )

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
                refuse(
                    f"  [red]omnigraph download failed[/red] ({exc}); install manually",
                    f"omnigraph download failed: {exc}",
                )
                return
            # ★ VERIFY BEFORE EXTRACTING, and refuse rather than warn. Nothing
            # checked these bytes before this: the installer put whatever the
            # URL returned onto a developer's PATH. On a moving tag it is also
            # the only thing tying the binary to the build this repo was tested
            # against — see _OMNIGRAPH_ASSET_SHA256.
            expected = _OMNIGRAPH_ASSET_SHA256.get(asset)
            if expected is None:
                refuse(
                    f"  [red]omnigraph[/red] — no pinned checksum for {asset}; "
                    "refusing to install an unverified binary",
                    f"no pinned checksum for {asset}; refusing to install an "
                    "unverified binary. Add its digest to "
                    "_OMNIGRAPH_ASSET_SHA256.",
                )
                return
            actual = hashlib.sha256(archive.read_bytes()).hexdigest()
            if actual != expected:
                refuse(
                    f"  [red]omnigraph checksum mismatch[/red] for {asset}\n"
                    f"    expected {expected}\n    got      {actual}\n"
                    f"  The '{_OMNIGRAPH_RELEASE_TAG}' tag has moved, or the "
                    "download was corrupted. Refresh the pinned digest "
                    "deliberately — do not install this.",
                    f"omnigraph checksum mismatch for {asset}: expected "
                    f"{expected}, got {actual}. The "
                    f"'{_OMNIGRAPH_RELEASE_TAG}' tag has moved, or the download "
                    "was corrupted. Refresh the pinned digest in "
                    "witan_core/omnigraph_install.py deliberately — do not "
                    "install this.",
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
            refuse(
                "  [red]omnigraph[/red] — binary not found in archive; "
                "install manually",
                f"no omnigraph binary inside {asset}; the release asset layout "
                "changed.",
            )
    except OmnigraphInstallFailed:
        # Already reported by `refuse`; re-wrapping it in the catch-all below
        # would bury the specific reason under a generic "download failed".
        raise
    except Exception as exc:  # noqa: BLE001
        refuse(
            f"  [red]omnigraph download failed[/red] ({exc}); install manually",
            f"omnigraph install failed: {exc}",
        )
    finally:
        tmp_dest.unlink(missing_ok=True)
