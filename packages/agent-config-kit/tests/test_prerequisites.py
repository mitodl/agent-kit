"""``AgentPlatform.mcp_conditional_on`` surfacing: Pi reads MCP servers only
while its built-in MCP extension or the third-party pi-mcp-adapter package is
loaded, so a run that plans Pi MCP entries must say so — with a read-only
preflight of Pi's settings files — on dry-run and real apply alike. Every
test runs against a tmp home."""

import json
from pathlib import Path

import pytest

from agent_config_kit.adapters.pi import mcp_prerequisite
from agent_config_kit.models import Scope, SkillSource, StdioServer
from agent_config_kit.plan import Prerequisite, RegistrationBundle, apply
from agent_config_kit.prune import PlatformState, apply_with_prune


def _bundle(**overrides) -> RegistrationBundle:
    defaults = {
        "mcp_servers": {"witan": StdioServer(command="uvx", args=["witan", "serve"])},
    }
    defaults.update(overrides)
    return RegistrationBundle(**defaults)


def _write_settings(
    path: Path, packages: list | None = None, extensions: list | None = None
) -> None:
    cfg = {}
    if packages is not None:
        cfg["packages"] = packages
    if extensions is not None:
        cfg["extensions"] = extensions
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg))


# What pi-mcp-adapter writes when it is installed, and what `pi config`
# writes to turn the built-in off. The adapter tests below add it so that
# only the adapter can satisfy the check.
_BUILTIN_OFF = ["-builtin:mcp"]


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def _global_settings(home: Path) -> Path:
    return home / ".pi" / "agent" / "settings.json"


# ── preflight ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "entry",
    [
        "npm:pi-mcp-adapter",
        "npm:pi-mcp-adapter@2.37.0",
        "git:github.com/nicobailon/pi-mcp-adapter@v2",
        "https://github.com/nicobailon/pi-mcp-adapter.git",
        "/home/someone/src/pi-mcp-adapter",
        {"source": "npm:pi-mcp-adapter", "skills": []},
    ],
)
def test_preflight_finds_adapter_in_global_settings(home, entry):
    _write_settings(_global_settings(home), ["npm:pi-subagents", entry], _BUILTIN_OFF)

    satisfied, detail = mcp_prerequisite(Scope.GLOBAL)

    assert satisfied is True
    assert str(_global_settings(home)) in detail


@pytest.mark.parametrize(
    "packages",
    [
        [],
        ["npm:pi-subagents", "npm:pi-mcp-adapter-fork"],
        [{"source": "npm:pi-mcp-adapter", "extensions": []}],  # extensions off
        [42, None, {"source": 7}],  # malformed entries are ignored
    ],
)
def test_preflight_reports_adapter_absent_with_builtin_off(home, packages):
    _write_settings(_global_settings(home), packages, _BUILTIN_OFF)

    satisfied, detail = mcp_prerequisite(Scope.GLOBAL)

    assert satisfied is False
    assert "built-in MCP is turned off in" in detail
    assert str(_global_settings(home)) in detail
    assert "pi config" in detail
    assert "pi install npm:pi-mcp-adapter" in detail
    assert "pi mcp list" in detail


@pytest.mark.parametrize("settings", [None, "not json", "[]", "{}"])
def test_preflight_builtin_is_on_by_default(home, settings):
    """Pi loads its built-in extensions unless settings turn them off, so
    missing, unparseable or silent settings leave the built-in MCP on."""
    if settings is not None:
        _global_settings(home).parent.mkdir(parents=True)
        _global_settings(home).write_text(settings)

    satisfied, detail = mcp_prerequisite(Scope.GLOBAL)

    assert satisfied is True
    assert "built-in MCP is not turned off" in detail
    assert "Pi 0.99" in detail


@pytest.mark.parametrize(
    ("extensions", "satisfied"),
    [
        (["-builtin:mcp"], False),
        (["!builtin:mcp"], False),
        (["+builtin:mcp"], True),
        (["builtin:mcp"], True),
        (["-builtin:mcp", "+builtin:mcp"], True),  # the last entry wins
        (["+builtin:mcp", "-builtin:mcp"], False),
        (["-builtin:codemode", "-builtin:mcpx", 7], True),  # other entries
    ],
)
def test_preflight_reads_the_builtin_mcp_setting(home, extensions, satisfied):
    _write_settings(_global_settings(home), extensions=extensions)

    assert mcp_prerequisite(Scope.GLOBAL)[0] is satisfied


def test_preflight_names_the_file_that_turns_the_builtin_on(home):
    _write_settings(_global_settings(home), extensions=["+builtin:mcp"])

    satisfied, detail = mcp_prerequisite(Scope.GLOBAL)

    assert satisfied is True
    assert f"turned on in {_global_settings(home)}" in detail


def test_preflight_adapter_counts_even_with_builtin_off(home):
    """pi-mcp-adapter turns the built-in off when it is installed, and then
    reads mcp.json itself, so that pairing is the adapter's normal state."""
    _write_settings(_global_settings(home), ["npm:pi-mcp-adapter"], _BUILTIN_OFF)

    satisfied, detail = mcp_prerequisite(Scope.GLOBAL)

    assert satisfied is True
    assert "pi-mcp-adapter is declared in" in detail


@pytest.mark.parametrize(
    ("global_ext", "project_ext", "satisfied"),
    [
        (["-builtin:mcp"], ["+builtin:mcp"], True),
        (["+builtin:mcp"], ["-builtin:mcp"], False),
        (None, ["-builtin:mcp"], False),
        (["-builtin:mcp"], None, False),
    ],
)
def test_preflight_project_builtin_setting_overrides_global(
    home, tmp_path, monkeypatch, global_ext, project_ext, satisfied
):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    if global_ext is not None:
        _write_settings(_global_settings(home), extensions=global_ext)
    if project_ext is not None:
        _write_settings(repo / ".pi" / "settings.json", extensions=project_ext)

    assert mcp_prerequisite(Scope.PROJECT)[0] is satisfied


def test_preflight_global_scope_ignores_project_builtin_setting(
    home, tmp_path, monkeypatch
):
    """Turning the built-in on in one project does not make
    ~/.pi/agent/mcp.json load in every other project."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    _write_settings(_global_settings(home), extensions=_BUILTIN_OFF)
    _write_settings(repo / ".pi" / "settings.json", extensions=["+builtin:mcp"])

    assert mcp_prerequisite(Scope.GLOBAL)[0] is False


def test_invalid_utf8_elsewhere_does_not_hide_the_adapter(home):
    """Pi reads settings.json with invalid bytes replaced (and a BOM
    stripped), so a stray non-UTF-8 byte in an unrelated value still leaves
    the adapter loaded; the preflight must agree rather than advise
    installing it again."""
    settings = _global_settings(home)
    settings.parent.mkdir(parents=True)
    settings.write_bytes(
        b'\xef\xbb\xbf{"theme": "caf\xe9", "packages": ["npm:pi-mcp-adapter"],'
        b' "extensions": ["-builtin:mcp"]}'
    )

    assert mcp_prerequisite(Scope.GLOBAL)[0] is True


@pytest.mark.parametrize("dry_run", [True, False])
def test_unopenable_pi_settings_do_not_abort_apply(home, monkeypatch, dry_run):
    """The preflight runs after mcp.json is written; a settings file it
    cannot open (e.g. root-owned after ``sudo pi install``) must be reported
    as unreadable, not raise out of apply before hooks and skills are
    installed, and not be misreported as the adapter being missing."""
    _write_settings(_global_settings(home), ["npm:pi-mcp-adapter"])
    real_read_text = Path.read_text

    def deny(self, *args, **kwargs):
        if self.name == "settings.json":
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny)

    satisfied, detail = mcp_prerequisite(Scope.GLOBAL)
    assert satisfied is False
    assert detail.startswith("could not read")
    assert "was not found" not in detail
    [prereq] = apply("pi", _bundle(), dry_run=dry_run).prerequisites
    assert prereq.satisfied is False


def test_preflight_accepts_extension_path_setting(home):
    path = _global_settings(home)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"extensions": ["-builtin:mcp", "~/src/pi-mcp-adapter"]})
    )

    assert mcp_prerequisite(Scope.GLOBAL)[0] is True


def test_preflight_project_scope_accepts_project_declaration(
    home, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    _write_settings(_global_settings(home), extensions=_BUILTIN_OFF)
    _write_settings(repo / ".pi" / "settings.json", ["npm:pi-mcp-adapter"])

    satisfied, detail = mcp_prerequisite(Scope.PROJECT)

    assert satisfied is True
    assert ".pi/settings.json" in detail


def test_preflight_global_scope_ignores_project_only_declaration(
    home, tmp_path, monkeypatch
):
    """A project-local install does not make ~/.pi/agent/mcp.json live in
    every other project, so global scope must not count it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    _write_settings(_global_settings(home), extensions=_BUILTIN_OFF)
    _write_settings(repo / ".pi" / "settings.json", ["npm:pi-mcp-adapter"])

    assert mcp_prerequisite(Scope.GLOBAL)[0] is False


def test_preflight_project_scope_absent_mentions_local_install(
    home, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    _write_settings(_global_settings(home), extensions=_BUILTIN_OFF)

    satisfied, detail = mcp_prerequisite(Scope.PROJECT)

    assert satisfied is False
    assert "pi install npm:pi-mcp-adapter -l" in detail


# ── plan.apply ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("dry_run", [True, False])
def test_pi_mcp_apply_surfaces_missing_mcp_support(home, dry_run):
    _write_settings(_global_settings(home), extensions=_BUILTIN_OFF)

    result = apply("pi", _bundle(), dry_run=dry_run)

    [prereq] = result.prerequisites
    assert prereq.capability == "mcp"
    assert prereq.satisfied is False
    assert prereq.needs_attention
    assert "built-in MCP" in prereq.requirement
    assert "pi-mcp-adapter" in prereq.requirement
    message = prereq.message(dry_run=dry_run)
    assert ("would be written" if dry_run else "were written") in message
    assert "pi config" in message
    assert "pi install npm:pi-mcp-adapter" in message


def test_pi_mcp_apply_dry_run_and_real_apply_report_the_same_prerequisite(home):
    _write_settings(_global_settings(home), extensions=_BUILTIN_OFF)

    dry = apply("pi", _bundle(), dry_run=True).prerequisites
    real = apply("pi", _bundle()).prerequisites

    assert dry == real


@pytest.mark.parametrize(
    ("packages", "extensions", "found"),
    [
        (["npm:pi-mcp-adapter"], _BUILTIN_OFF, "pi-mcp-adapter is declared in"),
        (None, None, "built-in MCP is not turned off"),
    ],
)
def test_pi_mcp_apply_reports_mcp_support_found(home, packages, extensions, found):
    _write_settings(_global_settings(home), packages, extensions)

    [prereq] = apply("pi", _bundle()).prerequisites

    assert prereq.satisfied is True
    assert not prereq.needs_attention
    message = prereq.message(dry_run=False)
    assert "Prerequisite found" in message
    assert found in message


def test_pi_prerequisite_is_carried_through_prune(home):
    _write_settings(_global_settings(home), extensions=_BUILTIN_OFF)

    result, _ = apply_with_prune("pi", _bundle(), PlatformState(), dry_run=True)

    assert [p.satisfied for p in result.prerequisites] == [False]


def test_no_prerequisite_when_bundle_has_no_mcp_servers(home, tmp_path):
    skill_md = tmp_path / "src" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("# skill")

    result = apply(
        "pi",
        _bundle(
            mcp_servers={},
            skills=[SkillSource(name="my-skill", skill_md_path=skill_md)],
        ),
    )

    assert result.planned  # the skill was still planned
    assert result.prerequisites == []


@pytest.mark.parametrize("platform", ["claude", "copilot", "opencode"])
def test_no_prerequisite_for_platforms_with_native_mcp(home, platform):
    assert apply(platform, _bundle(), dry_run=True).prerequisites == []


def test_prerequisite_without_a_check_is_unverified():
    prereq = Prerequisite(capability="mcp", requirement="requires a plugin")

    assert prereq.needs_attention
    assert "Could not verify" in prereq.message(dry_run=True)


# ── CLI rendering ────────────────────────────────────────────────────────────


def _run(app, args: list[str]) -> int:
    with pytest.raises(SystemExit) as exc_info:
        app(args)
    return exc_info.value.code


def _flat(text: str) -> str:
    # Rich wraps at the (hermetically pinned) terminal width; compare on
    # whitespace-normalized text so a wrap inside a phrase doesn't matter.
    return " ".join(text.split())


_MANIFEST = """
[mcp_servers.witan]
kind = "stdio"
command = "uvx"
"""


@pytest.mark.parametrize("dry_run", [True, False])
def test_cli_apply_warns_when_pi_has_no_mcp_support(home, tmp_path, capsys, dry_run):
    from agent_config_kit.cli import app

    _write_settings(_global_settings(home), extensions=_BUILTIN_OFF)
    manifest = tmp_path / "agent-config.toml"
    manifest.write_text(_MANIFEST)
    args = ["apply", str(manifest), "--platform", "pi"]
    if dry_run:
        args.append("--dry-run")

    assert _run(app, args) == 0  # a warning, not a failure

    out = _flat(capsys.readouterr().out)
    assert "⚠" in out
    assert "needs Pi's built-in MCP (Pi 0.99 or later) or the pi-mcp-adapter" in out
    assert "pi config" in out
    assert "pi install npm:pi-mcp-adapter" in out
    assert ("would be written" if dry_run else "were written") in out


@pytest.mark.parametrize(
    ("packages", "extensions"),
    [(["npm:pi-mcp-adapter"], _BUILTIN_OFF), (None, None)],
)
def test_cli_apply_notes_when_pi_mcp_support_present(
    home, tmp_path, capsys, packages, extensions
):
    from agent_config_kit.cli import app

    _write_settings(_global_settings(home), packages, extensions)
    manifest = tmp_path / "agent-config.toml"
    manifest.write_text(_MANIFEST)

    assert _run(app, ["apply", str(manifest), "--platform", "pi"]) == 0

    out = _flat(capsys.readouterr().out)
    assert "⚠" not in out
    assert "Prerequisite found" in out


def test_cli_apply_claude_shows_no_prerequisite(home, tmp_path, capsys):
    from agent_config_kit.cli import app

    manifest = tmp_path / "agent-config.toml"
    manifest.write_text(_MANIFEST)

    assert _run(app, ["apply", str(manifest), "--platform", "claude"]) == 0

    out = _flat(capsys.readouterr().out)
    assert "pi-mcp-adapter" not in out
    assert "⚠" not in out
