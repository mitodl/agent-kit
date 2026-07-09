"""Tests for witan-code's registration bundle and standalone omnigraph installer.

Mirrors mcp/servers/witan/tests/test_setup.py's coverage of the equivalent
witan functions — the two are deliberately duplicated (no cross-package
import, see setup.py's docstring), so their tests are duplicated too. Generic
install-mechanics behavior (dry-run no-op, additive merge, JSON skip-not-crash,
hook dedup, ...) is covered by agent-config-kit's own test suite
(``packages/agent-config-kit/tests/``) — the bundle tests here only assert
that ``witan_code_bundle()`` + ``apply("claude", ...)`` produces witan-code's
own MCP entry and hook commands, i.e. that the wiring is correct.
"""

import io
import json
import re
import tarfile
from pathlib import Path

from agent_config_kit import apply

from witan_code import setup

_OMNIGRAPH_VERSION_RE = re.compile(r'_OMNIGRAPH_VERSION = "([^"]+)"')


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


def test_install_omnigraph_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(setup.platform, "system", lambda: "Linux")
    monkeypatch.setattr(setup.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry-run hit network")),
    )

    setup.install_omnigraph(dry_run=True)

    assert not _dest(tmp_path).exists()


def test_install_omnigraph_unsupported_platform_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(setup.platform, "system", lambda: "plan9")
    monkeypatch.setattr(setup.platform, "machine", lambda: "mips")

    setup.install_omnigraph(dry_run=False)

    assert not _dest(tmp_path).exists()


def test_install_omnigraph_downloads_and_extracts(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(setup.platform, "system", lambda: "Linux")
    monkeypatch.setattr(setup.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: _FakeResponse(_fake_release_tarball(b"#!/bin/sh\necho fake")),
    )

    setup.install_omnigraph(dry_run=False)

    dest = _dest(tmp_path)
    assert dest.read_bytes() == b"#!/bin/sh\necho fake"
    assert dest.stat().st_mode & 0o111  # executable


def _write_fake_binary(dest: Path, version: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(f"#!/bin/sh\necho 'omnigraph {version}'\n")
    dest.chmod(0o755)


def test_install_omnigraph_skips_when_version_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_fake_binary(_dest(tmp_path), setup._OMNIGRAPH_VERSION)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("hit network")),
    )

    setup.install_omnigraph(dry_run=False)

    assert (
        _dest(tmp_path).read_text()
        == f"#!/bin/sh\necho 'omnigraph {setup._OMNIGRAPH_VERSION}'\n"
    )


def test_install_omnigraph_redownloads_when_version_differs(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(setup.platform, "system", lambda: "Linux")
    monkeypatch.setattr(setup.platform, "machine", lambda: "x86_64")
    _write_fake_binary(_dest(tmp_path), "0.1.0")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: _FakeResponse(_fake_release_tarball(b"#!/bin/sh\necho fake")),
    )

    setup.install_omnigraph(dry_run=False)

    assert _dest(tmp_path).read_bytes() == b"#!/bin/sh\necho fake"


def test_installed_version_none_on_timeout(tmp_path, monkeypatch):
    import subprocess

    dest = _dest(tmp_path)
    _write_fake_binary(dest, setup._OMNIGRAPH_VERSION)
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd=str(dest), timeout=10)
        ),
    )

    assert setup._installed_version(dest) is None


def test_installed_version_none_on_nonzero_exit(tmp_path):
    dest = _dest(tmp_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(f"#!/bin/sh\necho 'omnigraph {setup._OMNIGRAPH_VERSION}'\nexit 1\n")
    dest.chmod(0o755)

    assert setup._installed_version(dest) is None


# ── registration bundle ───────────────────────────────────────────────────────


def test_witan_code_bundle_registers_mcp_server_and_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()

    bundle = setup.witan_code_bundle(pkg_dir, "tester")
    apply("claude", bundle)

    claude_json = json.loads((tmp_path / ".claude.json").read_text())
    entry = claude_json["mcpServers"]["witan-code"]
    assert entry["type"] == "stdio"
    assert entry["command"] == "uvx"
    assert entry["env"]["WITAN_AUTHOR"] == "tester"

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    session_init = [
        h
        for e in settings["hooks"]["SessionStart"]
        for h in e["hooks"]
        if h["command"] == "witan-code session-init"
    ]
    reindex = [
        h
        for e in settings["hooks"]["PostToolUse"]
        for h in e["hooks"]
        if h["command"] == "witan-code reindex-hook"
    ]
    context = [
        h
        for e in settings["hooks"]["UserPromptSubmit"]
        for h in e["hooks"]
        if h["command"] == "witan-code inject-context"
    ]
    checkpoint = [
        h
        for e in settings["hooks"]["Stop"]
        for h in e["hooks"]
        if h["command"] == "witan-code checkpoint"
    ]
    assert session_init and reindex and context and checkpoint
    # Both prompt-path hooks carry a timeout so a hung git/store can't stall.
    assert context[0]["timeout"] == 15
    assert checkpoint[0]["timeout"] == 15


def test_witan_code_bundle_honors_binary_override(tmp_path):
    """witan.cli.setup_cmd passes binary="witan code" when folding this
    bundle into witan's own, so hooks only need `witan` on PATH — not a
    separately installed `witan-code` binary."""
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()

    bundle = setup.witan_code_bundle(pkg_dir, "tester", binary="witan code")

    commands = {h.command for h in bundle.hooks if hasattr(h, "command")}
    assert commands == {
        "witan code session-init",
        "witan code reindex-hook",
        "witan code inject-context",
        "witan code checkpoint",
    }


def test_witan_code_bundle_includes_pi_extensions_as_plugin_hooks(tmp_path):
    pkg_dir = tmp_path / "pkg"
    ext_dir = pkg_dir / "extensions" / "pi"
    ext_dir.mkdir(parents=True)
    (ext_dir / "codegraph.ts").write_text("// stub")

    bundle = setup.witan_code_bundle(pkg_dir, "tester")

    plugin_hooks = [h for h in bundle.hooks if hasattr(h, "entry_path")]
    assert any(h.entry_path.name == "codegraph.ts" for h in plugin_hooks)


def test_witan_code_bundle_includes_bundled_skills(tmp_path):
    pkg_dir = tmp_path / "pkg"
    skill_dir = pkg_dir / "skills" / "witan-code"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# witan-code")

    bundle = setup.witan_code_bundle(pkg_dir, "tester")

    assert any(s.name == "witan-code" for s in bundle.skills)


def test_omnigraph_version_matches_witan():
    """witan and witan-code each fetch their own omnigraph binary at runtime
    (no build-time bundling, no cross-package import — see setup.py's
    docstring), so nothing at import time enforces they stay pinned to the
    same release. renovate.json's customManager is supposed to bump both in
    one PR, but a manual edit to just one copy (exactly what caused a prior
    schema-version-mismatch outage) wouldn't touch the other — this is the
    CI-level backstop for that."""
    import pytest

    witan_setup = Path(__file__).parent.parent.parent / "witan" / "witan" / "setup.py"
    if not witan_setup.exists():
        pytest.skip("witan not present in this checkout")

    other_version = _OMNIGRAPH_VERSION_RE.search(witan_setup.read_text())
    assert other_version is not None, (
        f"couldn't find _OMNIGRAPH_VERSION in {witan_setup}"
    )
    assert setup._OMNIGRAPH_VERSION == other_version.group(1)
