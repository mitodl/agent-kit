import subprocess
from pathlib import Path

from agent_config_kit.config import GlobalConfig, OrgConfig, ScopeConfig
from agent_config_kit.models import Scope
from agent_config_kit.resolve import (
    find_repo_root,
    resolve_overlay,
    resolve_zero_arg_manifest,
)


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def test_find_repo_root_walks_up_to_the_nearest_git_dir(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)

    assert find_repo_root(nested) == repo


def test_find_repo_root_returns_none_outside_any_repo(tmp_path):
    assert find_repo_root(tmp_path) is None


def test_resolve_prefers_repo_local_manifest_over_scope_and_default(tmp_path):
    repo = tmp_path / "code" / "mit" / "myrepo"
    _init_git_repo(repo)
    local_manifest = repo / "agent-config.toml"
    local_manifest.write_text("")

    config = GlobalConfig(
        default_manifest=str(tmp_path / "dotfiles" / "agent-config.toml"),
        scope=[
            ScopeConfig(
                match_prefix=str(tmp_path / "code" / "mit"),
                manifest=str(tmp_path / "org.toml"),
                profiles=["platform-eng"],
            )
        ],
    )

    resolved = resolve_zero_arg_manifest(repo, config, tmp_path / "config.toml")

    assert resolved is not None
    assert resolved.path == local_manifest
    assert resolved.profiles is None
    assert "repo-local manifest" in resolved.source


def test_resolve_picks_longest_matching_scope_prefix(tmp_path):
    cwd = tmp_path / "code" / "mit" / "myrepo"
    cwd.mkdir(parents=True)

    config = GlobalConfig(
        scope=[
            ScopeConfig(
                match_prefix=str(tmp_path / "code"),
                manifest=str(tmp_path / "generic.toml"),
                profiles=["universal"],
            ),
            ScopeConfig(
                match_prefix=str(tmp_path / "code" / "mit"),
                manifest=str(tmp_path / "mit.toml"),
                profiles=["platform-eng"],
                write_scope=Scope.GLOBAL,
            ),
        ]
    )

    resolved = resolve_zero_arg_manifest(cwd, config, tmp_path / "config.toml")

    assert resolved is not None
    assert resolved.path == tmp_path / "mit.toml"
    assert resolved.profiles == ["platform-eng"]
    assert resolved.write_scope == Scope.GLOBAL


def test_resolve_does_not_match_a_sibling_directory_by_string_prefix(tmp_path):
    cwd = tmp_path / "code" / "mit-backup"
    cwd.mkdir(parents=True)

    config = GlobalConfig(
        scope=[
            ScopeConfig(
                match_prefix=str(tmp_path / "code" / "mit"),
                manifest=str(tmp_path / "mit.toml"),
            )
        ]
    )

    assert resolve_zero_arg_manifest(cwd, config, tmp_path / "config.toml") is None


def test_resolve_falls_back_to_default_manifest(tmp_path):
    cwd = tmp_path / "somewhere"
    cwd.mkdir()

    config = GlobalConfig(
        default_manifest=str(tmp_path / "dotfiles" / "agent-config.toml"),
        default_profiles=["universal"],
    )

    resolved = resolve_zero_arg_manifest(cwd, config, tmp_path / "config.toml")

    assert resolved is not None
    assert resolved.path == tmp_path / "dotfiles" / "agent-config.toml"
    assert resolved.profiles == ["universal"]
    assert resolved.source == "default_manifest"


def test_resolve_returns_none_when_nothing_matches(tmp_path):
    cwd = tmp_path / "somewhere"
    cwd.mkdir()

    assert (
        resolve_zero_arg_manifest(cwd, GlobalConfig(), tmp_path / "config.toml") is None
    )


def test_resolve_overlay_returns_empty_when_nothing_configured(tmp_path):
    overlay, source = resolve_overlay(GlobalConfig(), tmp_path / "config.toml")

    assert overlay == {}
    assert source == ""


def test_resolve_overlay_global_only():
    config = GlobalConfig(overlay={"mcp_servers": {"memory": {"kind": "stdio"}}})

    overlay, source = resolve_overlay(config, Path("/config.toml"))

    assert overlay == {"mcp_servers": {"memory": {"kind": "stdio"}}}
    assert source == "global"


def test_resolve_overlay_org_match_only():
    config = GlobalConfig()
    org_match = OrgConfig(
        name="mitodl",
        manifest="x",
        overlay={"skills": {"foo": "SKILL.md"}},
    )

    overlay, source = resolve_overlay(config, Path("/config.toml"), org_match=org_match)

    assert overlay == {"skills": {"foo": "SKILL.md"}}
    assert source == "org 'mitodl'"


def test_resolve_overlay_scope_match_only():
    config = GlobalConfig()
    scope_match = ScopeConfig(
        match_prefix="/code/mit",
        manifest="x",
        overlay={"mcp_servers": {"proxy": {"kind": "stdio"}}},
    )

    overlay, source = resolve_overlay(
        config, Path("/config.toml"), scope_match=scope_match
    )

    assert overlay == {"mcp_servers": {"proxy": {"kind": "stdio"}}}
    assert source == "scope '/code/mit'"


def test_resolve_overlay_global_wins_key_collision_against_org_match(tmp_path):
    config = GlobalConfig(
        overlay={"mcp_servers": {"witan": {"kind": "stdio", "command": "global"}}}
    )
    org_match = OrgConfig(
        name="mitodl",
        manifest="x",
        overlay={"mcp_servers": {"witan": {"kind": "stdio", "command": "org"}}},
    )

    overlay, source = resolve_overlay(
        config, tmp_path / "config.toml", org_match=org_match
    )

    assert overlay["mcp_servers"]["witan"]["command"] == "global"
    assert source == "org 'mitodl' + global"


def test_resolve_overlay_org_and_global_union_non_colliding_keys(tmp_path):
    config = GlobalConfig(overlay={"mcp_servers": {"memory": {"kind": "stdio"}}})
    org_match = OrgConfig(
        name="mitodl", manifest="x", overlay={"skills": {"foo": "SKILL.md"}}
    )

    overlay, _ = resolve_overlay(config, tmp_path / "config.toml", org_match=org_match)

    assert overlay == {
        "mcp_servers": {"memory": {"kind": "stdio"}},
        "skills": {"foo": "SKILL.md"},
    }


def test_resolve_zero_arg_manifest_carries_global_overlay_on_every_branch(tmp_path):
    """I3: the global overlay merges in unconditionally, regardless of
    which O2 branch resolves — proven here on the default_manifest branch,
    the one furthest from any org/scope match."""
    cwd = tmp_path / "somewhere"
    cwd.mkdir()
    config = GlobalConfig(
        default_manifest=str(tmp_path / "dotfiles" / "agent-config.toml"),
        overlay={"mcp_servers": {"memory": {"kind": "stdio"}}},
    )

    resolved = resolve_zero_arg_manifest(cwd, config, tmp_path / "config.toml")

    assert resolved is not None
    assert resolved.overlay == {"mcp_servers": {"memory": {"kind": "stdio"}}}
    assert resolved.overlay_source == "global"


def test_resolve_zero_arg_manifest_carries_matched_scope_overlay(tmp_path):
    cwd = tmp_path / "code" / "mit" / "myrepo"
    cwd.mkdir(parents=True)
    config = GlobalConfig(
        scope=[
            ScopeConfig(
                match_prefix=str(tmp_path / "code" / "mit"),
                manifest=str(tmp_path / "mit.toml"),
                overlay={"mcp_servers": {"proxy": {"kind": "stdio"}}},
            )
        ]
    )

    resolved = resolve_zero_arg_manifest(cwd, config, tmp_path / "config.toml")

    assert resolved is not None
    assert resolved.overlay == {"mcp_servers": {"proxy": {"kind": "stdio"}}}
    assert resolved.overlay_source == "scope " + repr(str(tmp_path / "code" / "mit"))


def test_resolve_zero_arg_manifest_repo_local_branch_gets_no_org_scope_overlay(
    tmp_path,
):
    """A repo-local manifest match carries no org/scope overlay (neither
    was consulted for this branch) — only the global layer, if any."""
    repo = tmp_path / "code" / "mit" / "myrepo"
    _init_git_repo(repo)
    (repo / "agent-config.toml").write_text("")
    config = GlobalConfig(
        scope=[
            ScopeConfig(
                match_prefix=str(tmp_path / "code" / "mit"),
                manifest=str(tmp_path / "mit.toml"),
                overlay={"mcp_servers": {"proxy": {"kind": "stdio"}}},
            )
        ]
    )

    resolved = resolve_zero_arg_manifest(repo, config, tmp_path / "config.toml")

    assert resolved is not None
    assert resolved.overlay == {}
    assert resolved.overlay_source == ""
