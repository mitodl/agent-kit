import subprocess
from pathlib import Path

from agent_config_kit.config import GlobalConfig, OrgConfig, ScopeConfig
from agent_config_kit.resolve import detect_org, resolve_zero_arg_manifest


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def _add_remote(path: Path, name: str, url: str) -> None:
    subprocess.run(["git", "remote", "add", name, url], cwd=path, check=True)


def test_detect_org_from_https_origin(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _add_remote(repo, "origin", "https://github.com/mitodl/agent-kit.git")

    assert detect_org(repo) == "mitodl"


def test_detect_org_from_ssh_origin(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _add_remote(repo, "origin", "git@github.com:mitodl/agent-kit.git")

    assert detect_org(repo) == "mitodl"


def test_detect_org_prefers_origin_over_other_remotes(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _add_remote(repo, "upstream", "https://github.com/other-org/agent-kit.git")
    _add_remote(repo, "origin", "https://github.com/mitodl/agent-kit.git")

    assert detect_org(repo) == "mitodl"


def test_detect_org_falls_back_to_other_remote_when_no_origin(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _add_remote(repo, "upstream", "https://github.com/mitodl/agent-kit.git")

    assert detect_org(repo) == "mitodl"


def test_detect_org_returns_none_for_non_github_remote(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _add_remote(repo, "origin", "https://gitlab.com/mitodl/agent-kit.git")

    assert detect_org(repo) is None


def test_detect_org_returns_none_with_no_remotes(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    assert detect_org(repo) is None


def test_detect_org_returns_none_outside_a_git_repo(tmp_path):
    assert detect_org(tmp_path) is None


def test_resolve_matches_org_from_git_remote(tmp_path):
    repo = tmp_path / "code" / "mit" / "myrepo"
    _init_git_repo(repo)
    _add_remote(repo, "origin", "https://github.com/mitodl/agent-kit.git")

    config = GlobalConfig(
        org=[
            OrgConfig(
                name="mitodl",
                manifest=str(tmp_path / "org.toml"),
                profiles=["platform-eng"],
            )
        ],
        default_manifest=str(tmp_path / "dotfiles" / "agent-config.toml"),
    )

    resolved = resolve_zero_arg_manifest(repo, config)

    assert resolved is not None
    assert resolved.path == tmp_path / "org.toml"
    assert resolved.profiles == ["platform-eng"]
    assert resolved.source == "org 'mitodl'"


def test_resolve_org_match_is_case_insensitive(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _add_remote(repo, "origin", "https://github.com/MITodl/agent-kit.git")

    config = GlobalConfig(
        org=[OrgConfig(name="mitodl", manifest=str(tmp_path / "org.toml"))]
    )

    resolved = resolve_zero_arg_manifest(repo, config)

    assert resolved is not None
    assert resolved.path == tmp_path / "org.toml"


def test_resolve_prefers_repo_local_manifest_over_org_match(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _add_remote(repo, "origin", "https://github.com/mitodl/agent-kit.git")
    local_manifest = repo / "agent-config.toml"
    local_manifest.write_text("")

    config = GlobalConfig(
        org=[OrgConfig(name="mitodl", manifest=str(tmp_path / "org.toml"))]
    )

    resolved = resolve_zero_arg_manifest(repo, config)

    assert resolved is not None
    assert resolved.path == local_manifest


def test_resolve_org_match_wins_over_scope_prefix(tmp_path):
    repo = tmp_path / "code" / "mit" / "myrepo"
    _init_git_repo(repo)
    _add_remote(repo, "origin", "https://github.com/mitodl/agent-kit.git")

    config = GlobalConfig(
        org=[OrgConfig(name="mitodl", manifest=str(tmp_path / "org.toml"))],
        scope=[
            ScopeConfig(
                match_prefix=str(tmp_path / "code" / "mit"),
                manifest=str(tmp_path / "scope.toml"),
            )
        ],
    )

    resolved = resolve_zero_arg_manifest(repo, config)

    assert resolved is not None
    assert resolved.path == tmp_path / "org.toml"


def test_resolve_falls_through_to_scope_prefix_when_org_does_not_match(tmp_path):
    repo = tmp_path / "code" / "mit" / "myrepo"
    _init_git_repo(repo)
    _add_remote(repo, "origin", "https://github.com/some-other-org/agent-kit.git")

    config = GlobalConfig(
        org=[OrgConfig(name="mitodl", manifest=str(tmp_path / "org.toml"))],
        scope=[
            ScopeConfig(
                match_prefix=str(tmp_path / "code" / "mit"),
                manifest=str(tmp_path / "scope.toml"),
            )
        ],
    )

    resolved = resolve_zero_arg_manifest(repo, config)

    assert resolved is not None
    assert resolved.path == tmp_path / "scope.toml"
