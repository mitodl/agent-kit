from pathlib import Path

import pytest

from agent_config_kit.manifest import ManifestError, load_manifest
from agent_config_kit.models import (
    DeclarativeHook,
    PluginRegistration,
    Scope,
    StdioServer,
)


def _write(tmp_path: Path, name: str, text: str) -> Path:
    manifest = tmp_path / name
    manifest.write_text(text)
    return manifest


def test_load_manifest_valid_round_trip(tmp_path):
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [options]
        scope = "global"
        platforms = ["claude", "pi"]

        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        args = ["witan", "serve"]
        env = { WITAN_AUTHOR = "team" }

        [mcp_servers.hosted-tool]
        kind = "remote"
        url = "https://example.com/mcp"

        [[hooks]]
        kind = "declarative"
        event = "user_prompt_submit"
        command = "witan inject-context"
        """,
    )

    result = load_manifest(manifest)

    assert result.path == manifest
    assert result.options.scope == Scope.GLOBAL
    assert result.options.platforms == ["claude", "pi"]
    witan = result.bundle.mcp_servers["witan"]
    assert isinstance(witan, StdioServer)
    assert witan.command == "uvx"
    assert witan.env == {"WITAN_AUTHOR": "team"}
    assert len(result.bundle.hooks) == 1
    assert isinstance(result.bundle.hooks[0], DeclarativeHook)


def test_load_manifest_defaults_scope_and_platforms(tmp_path):
    manifest = _write(tmp_path, "agent-config.toml", "")

    result = load_manifest(manifest)

    assert result.options.scope == Scope.GLOBAL
    assert result.options.platforms is None
    assert result.bundle.mcp_servers == {}


def test_load_manifest_resolves_plugin_entry_path_relative_to_manifest_dir(tmp_path):
    (tmp_path / "extensions" / "pi").mkdir(parents=True)
    (tmp_path / "extensions" / "pi" / "witan.ts").write_text("// stub")
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [[hooks]]
        kind = "plugin"
        entry_path = "extensions/pi/witan.ts"
        """,
    )

    result = load_manifest(manifest)

    hook = result.bundle.hooks[0]
    assert isinstance(hook, PluginRegistration)
    assert hook.entry_path == tmp_path / "extensions" / "pi" / "witan.ts"
    assert hook.entry_path.is_absolute()


def test_load_manifest_resolves_skill_md_path_relative_to_manifest_dir(tmp_path):
    (tmp_path / "skills" / "witan-task").mkdir(parents=True)
    (tmp_path / "skills" / "witan-task" / "SKILL.md").write_text("# skill")
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [skills]
        witan-task = "skills/witan-task/SKILL.md"
        """,
    )

    result = load_manifest(manifest)

    skill = result.bundle.skills[0]
    assert skill.name == "witan-task"
    assert skill.skill_md_path == tmp_path / "skills" / "witan-task" / "SKILL.md"


def test_load_manifest_resolves_skill_md_path_from_inline_table_form(tmp_path):
    (tmp_path / "skills" / "witan-task").mkdir(parents=True)
    (tmp_path / "skills" / "witan-task" / "SKILL.md").write_text("# skill")
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [skills]
        witan-task = { skill_md_path = "skills/witan-task/SKILL.md" }
        """,
    )

    result = load_manifest(manifest)

    skill = result.bundle.skills[0]
    assert skill.name == "witan-task"
    assert skill.skill_md_path == tmp_path / "skills" / "witan-task" / "SKILL.md"


def test_load_manifest_skill_table_form_requires_skill_md_path_key(tmp_path):
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [skills]
        witan-task = { some_future_field = "x" }
        """,
    )

    with pytest.raises(ManifestError, match="skills.witan-task.*skill_md_path"):
        load_manifest(manifest)


def test_load_manifest_skill_entry_must_be_string_or_table(tmp_path):
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [skills]
        witan-task = 123
        """,
    )

    with pytest.raises(ManifestError, match="skills.witan-task"):
        load_manifest(manifest)


def test_load_manifest_accepts_inline_array_of_tables_hooks(tmp_path):
    """F2: `hooks = [ {..}, {..} ]` must parse identically to the
    `[[hooks]]` array-of-tables header form — TOML produces the same
    list-of-dicts either way, so no model change is needed, just coverage."""
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        hooks = [
          { kind = "declarative", event = "stop", command = "witan session-checkpoint" },
          { kind = "plugin", entry_path = "extensions/pi/witan.ts" },
        ]
        """,
    )
    (tmp_path / "extensions" / "pi").mkdir(parents=True)
    (tmp_path / "extensions" / "pi" / "witan.ts").write_text("// stub")

    result = load_manifest(manifest)

    assert len(result.bundle.hooks) == 2
    assert isinstance(result.bundle.hooks[0], DeclarativeHook)
    assert isinstance(result.bundle.hooks[1], PluginRegistration)


def test_load_manifest_non_string_entry_path_raises_manifest_error(tmp_path):
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [[hooks]]
        kind = "plugin"
        entry_path = 123
        """,
    )

    with pytest.raises(ManifestError, match="entry_path"):
        load_manifest(manifest)


def test_load_manifest_non_string_skill_md_path_raises_manifest_error(tmp_path):
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [skills]
        witan-task = { skill_md_path = 123 }
        """,
    )

    with pytest.raises(ManifestError, match="skill_md_path"):
        load_manifest(manifest)


def test_load_manifest_fetches_remote_skill_md_path(tmp_path, monkeypatch):
    import agent_config_kit.manifest as manifest_module

    fetched = tmp_path / "cache" / "SKILL.md"
    fetched.parent.mkdir(parents=True)
    fetched.write_text("# remote skill")
    calls = []

    def fake_fetch(uri, cache_dir):
        calls.append((uri, cache_dir))
        return fetched

    monkeypatch.setattr(manifest_module, "fetch_remote", fake_fetch)
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [skills]
        witan-task = "https://raw.githubusercontent.com/org/repo/main/SKILL.md"
        """,
    )

    result = load_manifest(manifest)

    assert result.bundle.skills[0].skill_md_path == fetched
    assert calls == [
        (
            "https://raw.githubusercontent.com/org/repo/main/SKILL.md",
            tmp_path / ".agent-config-kit-cache",
        )
    ]


def test_load_manifest_fetches_remote_plugin_entry_path(tmp_path, monkeypatch):
    import agent_config_kit.manifest as manifest_module

    fetched = tmp_path / "cache" / "witan.ts"
    fetched.parent.mkdir(parents=True)
    fetched.write_text("// remote plugin")

    monkeypatch.setattr(manifest_module, "fetch_remote", lambda uri, cache_dir: fetched)
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [[hooks]]
        kind = "plugin"
        entry_path = "git+https://github.com/org/repo.git#subdirectory=witan.ts"
        """,
    )

    result = load_manifest(manifest)

    assert result.bundle.hooks[0].entry_path == fetched


def test_load_manifest_respects_explicit_cache_dir(tmp_path, monkeypatch):
    import agent_config_kit.manifest as manifest_module

    fetched = tmp_path / "elsewhere" / "SKILL.md"
    calls = []

    monkeypatch.setattr(
        manifest_module,
        "fetch_remote",
        lambda uri, cache_dir: calls.append(cache_dir) or fetched,
    )
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [skills]
        witan-task = "https://example.com/SKILL.md"
        """,
    )
    custom_cache = tmp_path / "custom-cache"

    load_manifest(manifest, cache_dir=custom_cache)

    assert calls == [custom_cache]


def test_load_manifest_wraps_fetch_error_as_manifest_error(tmp_path, monkeypatch):
    import agent_config_kit.manifest as manifest_module
    from agent_config_kit.fetch import FetchError

    def fake_fetch(uri, cache_dir):
        raise FetchError(f"could not fetch {uri}: HTTP 404")

    monkeypatch.setattr(manifest_module, "fetch_remote", fake_fetch)
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [skills]
        witan-task = "https://example.com/does-not-exist.md"
        """,
    )

    with pytest.raises(ManifestError, match="skills.witan-task.*404"):
        load_manifest(manifest)


def test_load_manifest_leaves_absolute_paths_unchanged(tmp_path):
    skill_md = tmp_path / "elsewhere" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("# skill")
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        f"""
        [skills]
        witan-task = "{skill_md.as_posix()}"
        """,
    )

    result = load_manifest(manifest)

    assert result.bundle.skills[0].skill_md_path == skill_md


def test_load_manifest_invalid_toml_raises_manifest_error(tmp_path):
    manifest = _write(tmp_path, "agent-config.toml", "this is not [valid toml")

    with pytest.raises(ManifestError, match="invalid TOML"):
        load_manifest(manifest)


def test_load_manifest_missing_required_field_raises_manifest_error(tmp_path):
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [mcp_servers.witan]
        kind = "stdio"
        """,
    )

    with pytest.raises(ManifestError, match="command"):
        load_manifest(manifest)


def test_load_manifest_bad_discriminator_raises_manifest_error(tmp_path):
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [[hooks]]
        kind = "bogus"
        command = "echo hi"
        """,
    )

    with pytest.raises(ManifestError):
        load_manifest(manifest)


def test_load_manifest_old_skills_array_form_raises_manifest_error(tmp_path):
    """The dropped [[skills]] array-of-tables form must fail with a clear
    ManifestError, not an AttributeError from calling .items() on a list
    (PR #76 review)."""
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [[skills]]
        name = "witan-task"
        skill_md_path = "skills/witan-task/SKILL.md"
        """,
    )

    with pytest.raises(ManifestError, match=r"\[skills\] must be a table"):
        load_manifest(manifest)


def test_load_manifest_non_table_skills_value_raises_manifest_error(tmp_path):
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        skills = "not-a-table"
        """,
    )

    with pytest.raises(ManifestError, match=r"\[skills\] must be a table"):
        load_manifest(manifest)


def test_load_manifest_unknown_top_level_key_raises_manifest_error(tmp_path):
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        mcp_server = {}
        """,
    )

    with pytest.raises(ManifestError, match="mcp_server"):
        load_manifest(manifest)


def test_load_manifest_unknown_platform_raises_manifest_error(tmp_path):
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [options]
        platforms = ["claude", "not-a-real-platform"]
        """,
    )

    with pytest.raises(ManifestError, match="not-a-real-platform"):
        load_manifest(manifest)


def test_load_manifest_missing_file_raises_manifest_error(tmp_path):
    with pytest.raises(ManifestError, match="could not read"):
        load_manifest(tmp_path / "does-not-exist.toml")


def test_instructions_before_tables_parses_as_top_level_key(tmp_path):
    """Regression fixture for the TOML gotcha documented in spec §3.1:
    a bare key must come before any [table]/[[array-of-tables]] header or
    TOML parses it as belonging to whichever table precedes it instead of
    as a top-level key."""
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        instructions = "See AGENTS.md"

        [[hooks]]
        kind = "declarative"
        event = "stop"
        command = "witan session-checkpoint"
        """,
    )

    result = load_manifest(manifest)

    assert result.bundle.instructions == "See AGENTS.md"


def test_instructions_after_table_is_absorbed_into_it_not_top_level(tmp_path):
    """The gotcha itself: placing `instructions` after a [[hooks]] header
    makes TOML treat it as a field of the last hook entry, not a top-level
    key. The hook model doesn't forbid extra fields, so it parses "cleanly"
    but silently drops the value — `bundle.instructions` stays None even
    though the manifest author intended to set it. This is why §3.1 says
    `instructions` must come before any table/array-of-tables header."""
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [[hooks]]
        kind = "declarative"
        event = "stop"
        command = "witan session-checkpoint"

        instructions = "See AGENTS.md"
        """,
    )

    result = load_manifest(manifest)

    assert result.bundle.instructions is None
