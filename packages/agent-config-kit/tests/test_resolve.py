import subprocess
from pathlib import Path

from agent_config_kit.config import GlobalConfig, ScopeConfig
from agent_config_kit.models import Scope
from agent_config_kit.resolve import find_repo_root, resolve_zero_arg_manifest


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

    resolved = resolve_zero_arg_manifest(repo, config)

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

    resolved = resolve_zero_arg_manifest(cwd, config)

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

    assert resolve_zero_arg_manifest(cwd, config) is None


def test_resolve_falls_back_to_default_manifest(tmp_path):
    cwd = tmp_path / "somewhere"
    cwd.mkdir()

    config = GlobalConfig(
        default_manifest=str(tmp_path / "dotfiles" / "agent-config.toml"),
        default_profiles=["universal"],
    )

    resolved = resolve_zero_arg_manifest(cwd, config)

    assert resolved is not None
    assert resolved.path == tmp_path / "dotfiles" / "agent-config.toml"
    assert resolved.profiles == ["universal"]
    assert resolved.source == "default_manifest"


def test_resolve_returns_none_when_nothing_matches(tmp_path):
    cwd = tmp_path / "somewhere"
    cwd.mkdir()

    assert resolve_zero_arg_manifest(cwd, GlobalConfig()) is None
