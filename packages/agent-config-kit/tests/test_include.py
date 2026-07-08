from pathlib import Path

import pytest

from agent_config_kit.manifest import ManifestError, load_manifest, resolve_profile


def _write(tmp_path: Path, name: str, text: str) -> Path:
    manifest = tmp_path / name
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(text)
    return manifest


def test_top_level_include_pulls_in_entries(tmp_path):
    _write(
        tmp_path,
        "base.toml",
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        include = ["base.toml"]

        [mcp_servers.hosted-tool]
        kind = "remote"
        url = "https://example.com/mcp"
        """,
    )

    result = load_manifest(manifest)

    assert set(result.bundle.mcp_servers) == {"witan", "hosted-tool"}


def test_include_path_resolved_relative_to_including_manifest(tmp_path):
    _write(
        tmp_path,
        "bundles/base.toml",
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )
    manifest = _write(
        tmp_path,
        "repo/agent-config.toml",
        """
        include = ["../bundles/base.toml"]
        """,
    )

    result = load_manifest(manifest)

    assert "witan" in result.bundle.mcp_servers


def test_local_entry_wins_over_included_entry_of_same_key(tmp_path):
    _write(
        tmp_path,
        "base.toml",
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "from-include"
        """,
    )
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        include = ["base.toml"]

        [mcp_servers.witan]
        kind = "stdio"
        command = "from-local"
        """,
    )

    result = load_manifest(manifest)

    assert result.bundle.mcp_servers["witan"].command == "from-local"


def test_later_include_wins_over_earlier_include(tmp_path):
    _write(
        tmp_path,
        "a.toml",
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "from-a"
        """,
    )
    _write(
        tmp_path,
        "b.toml",
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "from-b"
        """,
    )
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        include = ["a.toml", "b.toml"]
        """,
    )

    result = load_manifest(manifest)

    assert result.bundle.mcp_servers["witan"].command == "from-b"


def test_include_merges_hooks_by_identity(tmp_path):
    _write(
        tmp_path,
        "base.toml",
        """
        hooks = [
          { kind = "declarative", event = "stop", command = "shared" },
          { kind = "declarative", event = "session_start", command = "from-base" },
        ]
        """,
    )
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        include = ["base.toml"]

        hooks = [
          { kind = "declarative", event = "stop", command = "shared" },
        ]
        """,
    )

    result = load_manifest(manifest)

    commands = sorted(h.command for h in result.bundle.hooks)
    assert commands == ["from-base", "shared"]


def test_nested_includes_recurse_depth_first(tmp_path):
    _write(
        tmp_path,
        "grandparent.toml",
        """
        [mcp_servers.deepest]
        kind = "stdio"
        command = "uvx"
        """,
    )
    _write(
        tmp_path,
        "parent.toml",
        """
        include = ["grandparent.toml"]

        [mcp_servers.middle]
        kind = "stdio"
        command = "uvx"
        """,
    )
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        include = ["parent.toml"]
        """,
    )

    result = load_manifest(manifest)

    assert set(result.bundle.mcp_servers) == {"deepest", "middle"}


def test_include_cycle_raises_manifest_error(tmp_path):
    _write(
        tmp_path,
        "a.toml",
        """
        include = ["b.toml"]
        """,
    )
    _write(
        tmp_path,
        "b.toml",
        """
        include = ["a.toml"]
        """,
    )
    manifest = tmp_path / "a.toml"

    with pytest.raises(ManifestError, match="cycle"):
        load_manifest(manifest)


def test_include_must_be_a_list(tmp_path):
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        include = "base.toml"
        """,
    )

    with pytest.raises(ManifestError, match="include must be a list"):
        load_manifest(manifest)


def test_include_missing_target_raises_manifest_error(tmp_path):
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        include = ["does-not-exist.toml"]
        """,
    )

    with pytest.raises(ManifestError, match="does-not-exist.toml"):
        load_manifest(manifest)


def test_include_own_relative_skill_paths_resolve_against_its_own_dir(tmp_path):
    (tmp_path / "bundles" / "skills" / "commit").mkdir(parents=True)
    (tmp_path / "bundles" / "skills" / "commit" / "SKILL.md").write_text("# commit")
    _write(
        tmp_path,
        "bundles/base.toml",
        """
        [skills]
        commit = "skills/commit/SKILL.md"
        """,
    )
    manifest = _write(
        tmp_path,
        "repo/agent-config.toml",
        """
        include = ["../bundles/base.toml"]
        """,
    )

    result = load_manifest(manifest)

    assert result.bundle.skills[0].skill_md_path == (
        tmp_path / "bundles" / "skills" / "commit" / "SKILL.md"
    )


def test_included_manifests_own_options_are_ignored(tmp_path):
    _write(
        tmp_path,
        "base.toml",
        """
        [options]
        scope = "project"
        default_profiles = ["nope"]
        """,
    )
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        include = ["base.toml"]
        """,
    )

    result = load_manifest(manifest)

    assert result.options.default_profiles == []


def test_local_profile_of_same_name_overrides_included_profile_wholesale(tmp_path):
    _write(
        tmp_path,
        "base.toml",
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"

        [mcp_servers.other]
        kind = "stdio"
        command = "uvx"

        [profiles.team]
        mcp_servers = ["witan", "other"]
        """,
    )
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        include = ["base.toml"]

        [profiles.team]
        mcp_servers = ["witan"]
        """,
    )

    result = load_manifest(manifest)

    bundle = resolve_profile(result, ["team"])
    assert set(bundle.mcp_servers) == {"witan"}


def test_remote_include_fetched_via_fetch_remote(tmp_path, monkeypatch):
    import agent_config_kit.manifest as manifest_module

    fetched = tmp_path / "cache" / "base.toml"
    fetched.parent.mkdir(parents=True)
    fetched.write_text(
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """
    )
    calls = []

    def fake_fetch(uri, cache_dir):
        calls.append(uri)
        return fetched

    monkeypatch.setattr(manifest_module, "fetch_remote", fake_fetch)
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        include = ["https://example.com/bundles/base.toml"]
        """,
    )

    result = load_manifest(manifest)

    assert "witan" in result.bundle.mcp_servers
    assert calls == ["https://example.com/bundles/base.toml"]


def test_remote_include_wraps_fetch_error_as_manifest_error(tmp_path, monkeypatch):
    import agent_config_kit.manifest as manifest_module
    from agent_config_kit.fetch import FetchError

    def fake_fetch(uri, cache_dir):
        raise FetchError(f"could not fetch {uri}: HTTP 404")

    monkeypatch.setattr(manifest_module, "fetch_remote", fake_fetch)
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        include = ["https://example.com/bundles/base.toml"]
        """,
    )

    with pytest.raises(ManifestError, match="HTTP 404"):
        load_manifest(manifest)


def test_per_profile_include_selects_all_entries_of_included_manifest(tmp_path):
    _write(
        tmp_path,
        "frontend.toml",
        """
        hooks = [
          { kind = "declarative", event = "stop", command = "lint" },
        ]

        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"

        [skills]
        webapp-testing = "skills/webapp-testing/SKILL.md"
        """,
    )
    (tmp_path / "skills" / "webapp-testing").mkdir(parents=True)
    (tmp_path / "skills" / "webapp-testing" / "SKILL.md").write_text("# webapp-testing")
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [profiles.frontend]
        include = ["frontend.toml"]
        """,
    )

    result = load_manifest(manifest)
    bundle = resolve_profile(result, ["frontend"])

    assert set(bundle.mcp_servers) == {"witan"}
    assert {s.name for s in bundle.skills} == {"webapp-testing"}
    assert len(bundle.hooks) == 1


def test_per_profile_include_unions_with_explicit_lists(tmp_path):
    _write(
        tmp_path,
        "extra.toml",
        """
        [mcp_servers.extra-server]
        kind = "stdio"
        command = "uvx"
        """,
    )
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"

        [profiles.team]
        include = ["extra.toml"]
        mcp_servers = ["witan"]
        """,
    )

    result = load_manifest(manifest)
    bundle = resolve_profile(result, ["team"])

    assert set(bundle.mcp_servers) == {"witan", "extra-server"}


def test_per_profile_include_does_not_override_locally_declared_entry(tmp_path):
    _write(
        tmp_path,
        "extra.toml",
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "from-include"
        """,
    )
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "from-local"

        [profiles.team]
        include = ["extra.toml"]
        """,
    )

    result = load_manifest(manifest)

    assert result.bundle.mcp_servers["witan"].command == "from-local"


def test_per_profile_include_bypasses_included_manifests_own_profiles(tmp_path):
    """spec §5.2 clarification: a per-profile include selects ALL of the
    included manifest's top-level entries, not one of *its* profiles —
    there is no `URL#profile_name` fragment syntax in v1."""
    _write(
        tmp_path,
        "bundle.toml",
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"

        [mcp_servers.other]
        kind = "stdio"
        command = "uvx"

        [profiles.only-witan]
        mcp_servers = ["witan"]
        """,
    )
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [profiles.team]
        include = ["bundle.toml"]
        """,
    )

    result = load_manifest(manifest)
    bundle = resolve_profile(result, ["team"])

    assert set(bundle.mcp_servers) == {"witan", "other"}


def test_per_profile_include_cycle_raises_manifest_error(tmp_path):
    _write(
        tmp_path,
        "a.toml",
        """
        [profiles.p]
        include = ["agent-config.toml"]
        """,
    )
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        include = ["a.toml"]
        """,
    )

    with pytest.raises(ManifestError, match="cycle"):
        load_manifest(manifest)


def test_per_profile_include_non_string_ref_raises_manifest_error(tmp_path):
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [profiles.team]
        include = [123]
        """,
    )

    with pytest.raises(
        ManifestError, match="profiles.team.include entries must be strings"
    ):
        load_manifest(manifest)


def test_per_profile_include_non_list_ref_raises_manifest_error(tmp_path):
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [profiles.team]
        include = "base.toml"
        """,
    )

    with pytest.raises(ManifestError, match="profiles.team.include must be a list"):
        load_manifest(manifest)


def test_per_profile_include_with_malformed_selection_field_raises_manifest_error(
    tmp_path,
):
    """A profile that both has `include` and a malformed explicit selection
    list (a string instead of a list) must fail cleanly -- this used to
    reach `profile.setdefault(table_key, [])`, which doesn't correct an
    already-wrong-typed existing value, and crash with a raw AttributeError
    on `.append()`."""
    _write(
        tmp_path,
        "base.toml",
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [profiles.team]
        include = ["base.toml"]
        skills = "not-a-list"
        """,
    )

    with pytest.raises(ManifestError, match="profiles.team.skills must be a list"):
        load_manifest(manifest)


def test_non_table_mcp_servers_raises_manifest_error(tmp_path):
    manifest = _write(tmp_path, "agent-config.toml", 'mcp_servers = "oops"\n')

    with pytest.raises(ManifestError, match=r"\[mcp_servers\] must be a table"):
        load_manifest(manifest)


def test_non_table_lsp_servers_raises_manifest_error(tmp_path):
    manifest = _write(tmp_path, "agent-config.toml", 'lsp_servers = "oops"\n')

    with pytest.raises(ManifestError, match=r"\[lsp_servers\] must be a table"):
        load_manifest(manifest)


def test_non_table_profiles_raises_manifest_error(tmp_path):
    manifest = _write(tmp_path, "agent-config.toml", 'profiles = "oops"\n')

    with pytest.raises(ManifestError, match=r"\[profiles\] must be a table"):
        load_manifest(manifest)


def test_non_list_hooks_raises_manifest_error(tmp_path):
    manifest = _write(tmp_path, "agent-config.toml", 'hooks = "oops"\n')

    with pytest.raises(ManifestError, match="hooks must be a list"):
        load_manifest(manifest)


def test_hooks_list_with_non_table_entry_raises_manifest_error(tmp_path):
    manifest = _write(tmp_path, "agent-config.toml", 'hooks = ["oops"]\n')

    with pytest.raises(ManifestError, match=r"hooks\[0\] must be a table"):
        load_manifest(manifest)


def test_plugin_hook_missing_entry_path_raises_manifest_error(tmp_path):
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        [[hooks]]
        kind = "plugin"
        """,
    )

    with pytest.raises(ManifestError, match="missing 'entry_path'"):
        load_manifest(manifest)


def test_include_with_malformed_mcp_servers_in_included_file_raises_manifest_error(
    tmp_path,
):
    """The shape check must run for an included manifest too, not just the
    top-level one being loaded."""
    _write(tmp_path, "base.toml", 'mcp_servers = "oops"\n')
    manifest = _write(
        tmp_path,
        "agent-config.toml",
        """
        include = ["base.toml"]
        """,
    )

    with pytest.raises(ManifestError, match=r"\[mcp_servers\] must be a table"):
        load_manifest(manifest)
