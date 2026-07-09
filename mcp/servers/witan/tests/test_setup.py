"""Integration test for witan's own registration bundle.

Generic install-mechanics behavior (dry-run no-op, additive merge, JSON
skip-not-crash, hook dedup, ...) is covered by agent-config-kit's own test
suite (``packages/agent-config-kit/tests/``) — this only asserts that
``witan_bundle()`` + ``apply("claude", ...)`` produces *witan's* MCP entry and
hook commands, i.e. that the wiring is correct.
"""

import io
import json
import re
import tarfile
from pathlib import Path

import pytest
from agent_config_kit import apply

from witan import setup

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


def test_witan_bundle_registers_witan_mcp_server_and_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()

    bundle = setup.witan_bundle(pkg_dir, "tester")
    apply("claude", bundle)

    claude_json = json.loads((tmp_path / ".claude.json").read_text())
    entry = claude_json["mcpServers"]["witan"]
    assert entry["type"] == "stdio"
    assert entry["command"] == "uvx"
    assert entry["env"]["WITAN_AUTHOR"] == "tester"

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    inject = [
        h
        for e in settings["hooks"]["UserPromptSubmit"]
        for h in e["hooks"]
        if h["command"] == "witan inject-context"
    ]
    checkpoint = [
        h
        for e in settings["hooks"]["Stop"]
        for h in e["hooks"]
        if h["command"] == "witan session-checkpoint"
    ]
    assert inject and checkpoint
    # Both prompt-path hooks carry a timeout so a hung git/graph can't stall.
    assert inject[0]["timeout"] == 15
    assert checkpoint[0]["timeout"] == 15


def test_witan_bundle_includes_pi_extensions_as_plugin_hooks(tmp_path):
    pkg_dir = tmp_path / "pkg"
    ext_dir = pkg_dir / "extensions" / "pi"
    ext_dir.mkdir(parents=True)
    (ext_dir / "witan.ts").write_text("// stub")

    bundle = setup.witan_bundle(pkg_dir, "tester")

    plugin_hooks = [h for h in bundle.hooks if hasattr(h, "entry_path")]
    assert any(h.entry_path.name == "witan.ts" for h in plugin_hooks)


def test_witan_bundle_includes_bundled_skills(tmp_path):
    pkg_dir = tmp_path / "pkg"
    skill_dir = pkg_dir / "skills" / "witan-task"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# witan-task")

    bundle = setup.witan_bundle(pkg_dir, "tester")

    assert any(s.name == "witan-task" for s in bundle.skills)


def test_omnigraph_version_matches_witan_code():
    """witan and witan-code each fetch their own omnigraph binary at runtime
    (no build-time bundling, no cross-package import — see setup.py's
    docstring), so nothing at import time enforces they stay pinned to the
    same release. renovate.json's customManager is supposed to bump both in
    one PR, but a manual edit to just one copy (exactly what caused a prior
    schema-version-mismatch outage) wouldn't touch the other — this is the
    CI-level backstop for that."""
    witan_code_setup = (
        Path(__file__).parent.parent.parent / "witan-code" / "witan_code" / "setup.py"
    )
    if not witan_code_setup.exists():
        pytest.skip("witan-code not present in this checkout")

    other_version = _OMNIGRAPH_VERSION_RE.search(witan_code_setup.read_text())
    assert other_version is not None, (
        f"couldn't find _OMNIGRAPH_VERSION in {witan_code_setup}"
    )
    assert setup._OMNIGRAPH_VERSION == other_version.group(1)


def test_install_default_config_writes_starter_file(tmp_path, monkeypatch):
    import tomllib

    from witan import config as cfg_module

    dest = tmp_path / "config.toml"
    monkeypatch.setattr(cfg_module, "DEFAULT_CONFIG_PATH", dest)

    setup.install_default_config(dry_run=False)

    assert dest.exists()
    parsed = tomllib.loads(dest.read_text())
    assert parsed == {"rank": {}, "scan": {}}  # everything ships commented out


def test_install_default_config_skips_existing_file(tmp_path, monkeypatch):
    from witan import config as cfg_module

    dest = tmp_path / "config.toml"
    dest.write_text("author = 'do not touch'\n")
    monkeypatch.setattr(cfg_module, "DEFAULT_CONFIG_PATH", dest)

    setup.install_default_config(dry_run=False)

    assert dest.read_text() == "author = 'do not touch'\n"


def test_install_default_config_dry_run_writes_nothing(tmp_path, monkeypatch):
    from witan import config as cfg_module

    dest = tmp_path / "config.toml"
    monkeypatch.setattr(cfg_module, "DEFAULT_CONFIG_PATH", dest)

    setup.install_default_config(dry_run=True)

    assert not dest.exists()


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


# ── Legacy workflow-hook prune (A1 double-emission fix) ──────────────────────


def _settings_with(commands_by_event):
    return {
        "hooks": {
            event: [
                {"matcher": "", "hooks": [{"type": "command", "command": c}]}
                for c in commands
            ]
            for event, commands in commands_by_event.items()
        }
    }


def test_prune_removes_wrapper_keeps_bare():
    settings = _settings_with(
        {
            "UserPromptSubmit": [
                "witan inject-context",
                "bash ~/.claude/hooks/workflow-context-inject.sh",
            ],
            "Stop": ["bash ~/.claude/hooks/workflow-session-checkpoint.sh"],
        }
    )
    changed = setup.prune_legacy_hook_entries(settings)
    assert changed is True
    ups = settings["hooks"]["UserPromptSubmit"]
    assert [e["hooks"][0]["command"] for e in ups] == ["witan inject-context"]
    # Stop had only the wrapper → matcher entry dropped entirely, no empty hooks list.
    assert settings["hooks"]["Stop"] == []


def test_prune_matches_configs_hooks_form():
    settings = _settings_with(
        {
            "UserPromptSubmit": ["bash $REPO/configs/hooks/workflow-context-inject.sh"],
        }
    )
    assert setup.prune_legacy_hook_entries(settings) is True
    assert settings["hooks"]["UserPromptSubmit"] == []


def test_prune_idempotent_and_leaves_others():
    settings = _settings_with(
        {
            "UserPromptSubmit": ["witan inject-context"],
            "PostToolUse": ["witan-code reindex-hook"],
        }
    )
    assert setup.prune_legacy_hook_entries(settings) is False
    assert setup.prune_legacy_hook_entries(settings) is False
    assert len(settings["hooks"]["PostToolUse"]) == 1


def test_prune_no_hooks_section_is_noop():
    assert setup.prune_legacy_hook_entries({}) is False
