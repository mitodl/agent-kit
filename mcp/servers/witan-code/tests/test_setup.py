"""Tests for witan-code's registration bundle.

Generic install-mechanics behavior (dry-run no-op, additive merge, JSON
skip-not-crash, hook dedup, ...) is covered by agent-config-kit's own test
suite (``packages/agent-config-kit/tests/``); the shared omnigraph installer is
covered by ``packages/witan-core/tests/test_omnigraph_install.py``. The bundle
tests here only assert that ``witan_code_bundle()`` + ``apply("claude", ...)``
produces witan-code's own MCP entry and hook commands, i.e. that the wiring is
correct.
"""

import json
import re
from pathlib import Path

import pytest

from agent_config_kit import apply
from witan_code import setup


def test_witan_code_bundle_registers_mcp_server_and_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()

    bundle = setup.witan_code_bundle(pkg_dir, "tester")
    apply("claude", bundle)

    claude_json = json.loads((tmp_path / ".claude.json").read_text())
    entry = claude_json["mcpServers"]["witan-code"]
    assert entry["type"] == "stdio"
    # Claude Code's hooks already require the CLI on PATH, so the MCP entry runs
    # that same install rather than a second one resolved from git `main`.
    assert entry["command"] == "witan-code"
    assert entry["args"] == ["serve"]
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
    # The CLI-form MCP entry follows `binary` too, so it points at whichever
    # command the hooks were told to use.
    cli_entry = bundle.mcp_servers_by_platform["claude"]["witan-code"]
    assert (cli_entry.command, cli_entry.args) == ("witan", ["code", "serve"])


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


def test_setup_does_not_abort_on_a_refused_omnigraph_binary(tmp_path, monkeypatch):
    """★ Same contract as `witan setup`, and worth asserting separately.

    The installer raises by default as of witan-core 0.30.0, which is right for
    the workflow steps calling it through `python -c` — they used to swallow a
    checksum refusal and exit 0. It is wrong for an interactive command that
    also installs the agent bundles: aborting would cost the user those over a
    binary they can install separately, and the refusal is printed either way.

    `cli.setup` imports `install_omnigraph` inside the function body, so the
    patch target is witan_core's own attribute rather than a module-level name
    in cli.
    """
    import witan_core
    from witan_code import cli

    calls: list[dict] = []
    monkeypatch.setattr(
        witan_core,
        "install_omnigraph",
        lambda dry_run, **kwargs: calls.append({"dry_run": dry_run, **kwargs}),
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    cli.setup(agent="claude", author="tester")

    assert calls == [{"dry_run": False, "strict": False}]


def _pi_setup_output(tmp_path, monkeypatch, capsys, *, dry_run: bool) -> str:
    import witan_core
    from witan_code import cli

    monkeypatch.setattr(witan_core, "install_omnigraph", lambda dry_run, **kw: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    cli.setup(agent="pi", author="tester", dry_run=dry_run)

    return " ".join(capsys.readouterr().out.split())


def _pi_settings(tmp_path, cfg: dict) -> None:
    settings = tmp_path / ".pi" / "agent" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(cfg))


def test_setup_pi_warns_when_pi_cannot_load_mcp(tmp_path, monkeypatch, capsys):
    """`witan-code setup --agent pi` inherits agent-config-kit's MCP
    prerequisite preflight: Pi ignores ~/.pi/agent/mcp.json when its
    built-in MCP is turned off and pi-mcp-adapter is not installed, so the
    report must say so on dry-run and apply."""
    _pi_settings(tmp_path, {"extensions": ["-builtin:mcp"]})
    for dry_run in (True, False):
        out = _pi_setup_output(tmp_path, monkeypatch, capsys, dry_run=dry_run)
        assert "WARNING:" in out
        assert "pi install npm:pi-mcp-adapter" in out


def test_setup_pi_notes_when_pi_mcp_adapter_is_installed(tmp_path, monkeypatch, capsys):
    _pi_settings(
        tmp_path,
        {"packages": ["npm:pi-mcp-adapter"], "extensions": ["-builtin:mcp"]},
    )

    out = _pi_setup_output(tmp_path, monkeypatch, capsys, dry_run=True)

    assert "WARNING:" not in out
    assert "note:" in out
    assert "pi-mcp-adapter is declared in" in out


def test_setup_pi_notes_when_pi_builtin_mcp_is_on(tmp_path, monkeypatch, capsys):
    out = _pi_setup_output(tmp_path, monkeypatch, capsys, dry_run=True)

    assert "WARNING:" not in out
    assert "note:" in out
    assert "built-in MCP is not turned off" in out


def _dangling_pi_extension(tmp_path) -> Path:
    dest = tmp_path / ".pi" / "agent" / "extensions" / "codegraph.ts"
    dest.parent.mkdir(parents=True)
    dest.symlink_to(tmp_path / "gone" / "codegraph.ts")
    return dest


def test_setup_exits_with_force_hint_on_a_symlinked_pi_extension(
    tmp_path, monkeypatch, capsys
):
    import witan_core
    from witan_code import cli

    monkeypatch.setattr(witan_core, "install_omnigraph", lambda dry_run, **kw: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    dest = _dangling_pi_extension(tmp_path)

    with pytest.raises(SystemExit) as exc:
        cli.setup(agent="pi", author="tester")

    assert exc.value.code == 2
    assert "--force" in capsys.readouterr().out
    assert dest.is_symlink()


def test_setup_force_replaces_a_symlinked_pi_extension(tmp_path, monkeypatch):
    import witan_core
    from witan_code import cli

    monkeypatch.setattr(witan_core, "install_omnigraph", lambda dry_run, **kw: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    dest = _dangling_pi_extension(tmp_path)

    cli.setup(agent="pi", author="tester", force=True)

    assert dest.is_file()
    assert not dest.is_symlink()


# --- Pi extension source contract ------------------------------------------
#
# The Pi extension is TypeScript that no test here can execute, so these read
# its source. They pin two things a Python-side change cannot see drifting:
# the Pi prompt-injection budget matching the Claude hook's, and the three
# copies of the file (package, configs/pi mirror) staying byte-identical.

_PKG_EXT = Path(setup.__file__).parent / "extensions" / "pi" / "codegraph.ts"
_REPO_ROOT = Path(__file__).resolve().parents[4]
_CONFIG_EXT = _REPO_ROOT / "configs" / "pi" / "extensions" / "codegraph.ts"


def _ts_const_ms(source: str, name: str) -> int:
    match = re.search(rf"^const {name} = ([\d_]+);$", source, re.MULTILINE)
    assert match, f"{name} is not declared as a numeric const"
    return int(match.group(1).replace("_", ""))


def test_pi_inject_context_timeout_matches_the_claude_hook():
    """A 5s Pi budget killed the cold read every Claude prompt waits out."""
    source = _PKG_EXT.read_text()
    timeout_ms = _ts_const_ms(source, "INJECT_CONTEXT_TIMEOUT_MS")

    assert timeout_ms == setup.INJECT_CONTEXT_TIMEOUT_SECONDS * 1000
    assert setup.INJECT_CONTEXT_TIMEOUT_SECONDS >= 15
    # The one blocking call is the one that uses the constant; nothing else
    # hard-codes its own number.
    assert "timeout: INJECT_CONTEXT_TIMEOUT_MS" in source
    assert source.count("spawnSync(") == 1
    assert "timeout: 5000" not in source


def test_claude_inject_context_hook_uses_the_shared_constant(tmp_path):
    bundle = setup.witan_code_bundle(tmp_path, "tester")
    (hook,) = [
        h
        for h in bundle.hooks
        if getattr(h, "command", None) == "witan-code inject-context"
    ]
    assert hook.timeout_seconds == setup.INJECT_CONTEXT_TIMEOUT_SECONDS


def test_configs_pi_mirror_matches_the_package_extension():
    assert _CONFIG_EXT.read_bytes() == _PKG_EXT.read_bytes(), (
        f"{_CONFIG_EXT} drifted from {_PKG_EXT}; copy the package file over it"
    )


def test_pi_extension_asks_for_pi_rendered_context():
    """Without a Pi `--client` the block tells Pi to call a ToolSearch it
    lacks, and with the wrong one it names the other MCP support's calling
    convention: the extension must pick `pi` only when pi-mcp-adapter's
    `mcp` tool is registered."""
    source = _PKG_EXT.read_text()

    assert re.search(
        r'spawnSync\(\s*"witan-code",\s*\["inject-context", "--client", client\]',
        source,
    )
    assert "const client = piClient(pi);" in source
    assert 'pi.getAllTools().some((tool) => tool?.name === "mcp")' in source
    assert re.search(r'\?\s*"pi"\s*:\s*"pi-builtin"', source)
