from pathlib import Path

import pytest

from agent_config_kit.manifest import ManifestError, load_manifest, resolve_profile


def _write(tmp_path: Path, text: str) -> Path:
    manifest = tmp_path / "agent-config.toml"
    manifest.write_text(text)
    return manifest


_BASE_MANIFEST = """
hooks = [
  { kind = "declarative", event = "stop", command = "witan session-checkpoint" },
]

[mcp_servers.witan]
kind = "stdio"
command = "uvx"

[mcp_servers.hosted-tool]
kind = "remote"
url = "https://example.com/mcp"

[skills]
commit = "./skills/commit/SKILL.md"
webapp-testing = "./skills/webapp-testing/SKILL.md"
"""


def _write_skills(tmp_path: Path) -> None:
    for name in ("commit", "webapp-testing"):
        skill_dir = tmp_path / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"# {name}")


def test_resolve_profile_with_no_names_returns_full_bundle(tmp_path):
    _write_skills(tmp_path)
    manifest = _write(tmp_path, _BASE_MANIFEST)

    loaded = load_manifest(manifest)
    resolved = resolve_profile(loaded, [])

    assert resolved is loaded.bundle


def test_profile_selects_exactly_its_listed_entries(tmp_path):
    _write_skills(tmp_path)
    manifest = _write(
        tmp_path,
        _BASE_MANIFEST
        + """
        [profiles.universal]
        skills = ["commit"]
        mcp_servers = ["witan"]
        hooks = ["declarative:stop:witan session-checkpoint"]
        """,
    )

    loaded = load_manifest(manifest)
    resolved = resolve_profile(loaded, ["universal"])

    assert set(resolved.mcp_servers) == {"witan"}
    assert [s.name for s in resolved.skills] == ["commit"]
    assert len(resolved.hooks) == 1


def test_profile_inherits_transitively(tmp_path):
    _write_skills(tmp_path)
    manifest = _write(
        tmp_path,
        _BASE_MANIFEST
        + """
        [profiles.universal]
        skills = ["commit"]

        [profiles.frontend]
        inherits = ["universal"]
        skills = ["webapp-testing"]
        mcp_servers = ["witan"]
        """,
    )

    loaded = load_manifest(manifest)
    resolved = resolve_profile(loaded, ["frontend"])

    assert {s.name for s in resolved.skills} == {"commit", "webapp-testing"}
    assert set(resolved.mcp_servers) == {"witan"}


def test_selecting_multiple_profiles_is_a_union(tmp_path):
    _write_skills(tmp_path)
    manifest = _write(
        tmp_path,
        _BASE_MANIFEST
        + """
        [profiles.a]
        skills = ["commit"]

        [profiles.b]
        mcp_servers = ["witan"]
        """,
    )

    loaded = load_manifest(manifest)
    resolved = resolve_profile(loaded, ["a", "b"])

    assert {s.name for s in resolved.skills} == {"commit"}
    assert set(resolved.mcp_servers) == {"witan"}


def test_instructions_is_carried_through_regardless_of_profile_selection(tmp_path):
    _write_skills(tmp_path)
    manifest = _write(
        tmp_path,
        """
        instructions = "See AGENTS.md"

        """
        + _BASE_MANIFEST
        + """
        [profiles.universal]
        skills = ["commit"]
        """,
    )

    loaded = load_manifest(manifest)
    resolved = resolve_profile(loaded, ["universal"])

    assert resolved.instructions == "See AGENTS.md"


def test_resolve_profile_unknown_name_raises_manifest_error(tmp_path):
    _write_skills(tmp_path)
    manifest = _write(
        tmp_path,
        _BASE_MANIFEST
        + """
        [profiles.universal]
        skills = ["commit"]
        """,
    )

    loaded = load_manifest(manifest)

    with pytest.raises(ManifestError, match="no such profile"):
        resolve_profile(loaded, ["does-not-exist"])


def test_load_manifest_rejects_profile_referencing_unknown_skill(tmp_path):
    _write_skills(tmp_path)
    manifest = _write(
        tmp_path,
        _BASE_MANIFEST
        + """
        [profiles.universal]
        skills = ["not-a-real-skill"]
        """,
    )

    with pytest.raises(ManifestError, match="not-a-real-skill"):
        load_manifest(manifest)


def test_load_manifest_rejects_profile_referencing_unknown_mcp_server(tmp_path):
    _write_skills(tmp_path)
    manifest = _write(
        tmp_path,
        _BASE_MANIFEST
        + """
        [profiles.universal]
        mcp_servers = ["not-a-real-server"]
        """,
    )

    with pytest.raises(ManifestError, match="not-a-real-server"):
        load_manifest(manifest)


def test_load_manifest_rejects_profile_referencing_unknown_hook(tmp_path):
    _write_skills(tmp_path)
    manifest = _write(
        tmp_path,
        _BASE_MANIFEST
        + """
        [profiles.universal]
        hooks = ["declarative:stop:not-a-real-command"]
        """,
    )

    with pytest.raises(ManifestError, match="not-a-real-command"):
        load_manifest(manifest)


def test_load_manifest_rejects_profile_inheriting_unknown_profile(tmp_path):
    _write_skills(tmp_path)
    manifest = _write(
        tmp_path,
        _BASE_MANIFEST
        + """
        [profiles.frontend]
        inherits = ["does-not-exist"]
        """,
    )

    with pytest.raises(ManifestError, match="does-not-exist"):
        load_manifest(manifest)


def test_load_manifest_rejects_direct_profile_inheritance_cycle(tmp_path):
    _write_skills(tmp_path)
    manifest = _write(
        tmp_path,
        _BASE_MANIFEST
        + """
        [profiles.a]
        inherits = ["b"]

        [profiles.b]
        inherits = ["a"]
        """,
    )

    with pytest.raises(ManifestError, match="cycle"):
        load_manifest(manifest)


def test_load_manifest_rejects_self_inheriting_profile(tmp_path):
    _write_skills(tmp_path)
    manifest = _write(
        tmp_path,
        _BASE_MANIFEST
        + """
        [profiles.a]
        inherits = ["a"]
        """,
    )

    with pytest.raises(ManifestError, match="cycle"):
        load_manifest(manifest)


def test_load_manifest_parses_default_profiles_option(tmp_path):
    _write_skills(tmp_path)
    manifest = _write(
        tmp_path,
        _BASE_MANIFEST
        + """
        [profiles.universal]
        skills = ["commit"]

        [options]
        default_profiles = ["universal"]
        """,
    )

    loaded = load_manifest(manifest)

    assert loaded.options.default_profiles == ["universal"]


def test_load_manifest_rejects_unknown_default_profile(tmp_path):
    _write_skills(tmp_path)
    manifest = _write(
        tmp_path,
        _BASE_MANIFEST
        + """
        [options]
        default_profiles = ["does-not-exist"]
        """,
    )

    with pytest.raises(ManifestError, match="does-not-exist"):
        load_manifest(manifest)


def test_load_manifest_with_no_profiles_table_has_empty_profiles_dict(tmp_path):
    _write_skills(tmp_path)
    manifest = _write(tmp_path, _BASE_MANIFEST)

    loaded = load_manifest(manifest)

    assert loaded.profiles == {}
