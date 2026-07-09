"""End-to-end integration tests for the profiles/composition/scoped-
provisioning feature set (spec §10) — a whole zero-arg apply run through
the CLI, over a real filesystem layout, rather than unit-testing each
module in isolation. Fixture style matches test_cli.py's zero-arg tests and
test_plan.py's ``monkeypatch.setattr(Path, "home", ...)`` convention.
"""

import json
import subprocess
from pathlib import Path

import pytest


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _run_ok(app, args: list[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        app(args)
    assert exc_info.value.code == 0


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def _add_remote(path: Path, name: str, url: str) -> None:
    subprocess.run(["git", "remote", "add", name, url], cwd=path, check=True)


def test_profile_stacking_with_inherits_and_include_end_to_end(tmp_path, monkeypatch):
    """A profile that both `inherits` another profile and pulls in a shared
    bundle via top-level `include` resolves to the union of all three."""
    from agent_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write(
        tmp_path / "shared" / "base.toml",
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )
    repo = tmp_path / "repo"
    _write(
        repo / "agent-config.toml",
        """
        include = ["../shared/base.toml"]

        [mcp_servers.hosted-tool]
        kind = "remote"
        url = "https://example.com/mcp"

        [profiles.universal]
        mcp_servers = ["witan"]

        [profiles.frontend]
        inherits = ["universal"]
        mcp_servers = ["hosted-tool"]
        """,
    )
    monkeypatch.chdir(repo)

    _run_ok(
        app,
        [
            "apply",
            str(repo / "agent-config.toml"),
            "--platform",
            "claude",
            "--scope",
            "project",
            "--profile",
            "frontend",
        ],
    )

    servers = json.loads((repo / ".mcp.json").read_text())["mcpServers"]
    assert set(servers) == {"witan", "hosted-tool"}


def test_include_cycle_surfaces_as_clean_cli_error(tmp_path, monkeypatch, capsys):
    from agent_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write(tmp_path / "a.toml", 'include = ["b.toml"]\n')
    manifest = _write(tmp_path / "b.toml", 'include = ["a.toml"]\n')

    with pytest.raises(SystemExit) as exc_info:
        app(["apply", str(manifest), "--platform", "claude"])

    assert exc_info.value.code == 2
    assert "cycle" in capsys.readouterr().out


def test_zero_arg_apply_resolves_via_org_match_and_writes_project_scope(
    tmp_path, monkeypatch, capsys
):
    """Global config's `[[org]]` resolves an org-wide manifest purely from
    the repo's git remote, with no MANIFEST argument and no repo-local
    agent-config.toml — the full O2 step-3 path, materializing into the
    repo's own project-scope target (not the agent's global config)."""
    from agent_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("AC_KIT_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    org_manifest = _write(
        tmp_path / "org" / "agent-config.toml",
        """
        [options]
        scope = "project"

        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"

        [profiles.platform-eng]
        mcp_servers = ["witan"]
        """,
    )
    config_dir = tmp_path / ".config" / "agent-config-kit"
    _write(
        config_dir / "config.toml",
        f"""
        [[org]]
        name = "mitodl"
        manifest = "{org_manifest}"
        profiles = ["platform-eng"]
        """,
    )

    repo = tmp_path / "code" / "myrepo"
    _init_git_repo(repo)
    _add_remote(repo, "origin", "https://github.com/mitodl/agent-kit.git")
    monkeypatch.chdir(repo)

    _run_ok(app, ["apply", "--platform", "claude"])

    out = capsys.readouterr().out
    assert "resolved manifest from org 'mitodl'" in out
    assert (repo / ".mcp.json").is_file()
    assert not (tmp_path / ".mcp.json").is_file()


def test_zero_arg_apply_resolves_via_directory_prefix_when_org_does_not_match(
    tmp_path, monkeypatch, capsys
):
    """No repo-local manifest, no `[[org]]` match (a non-github remote) ->
    falls through to the longest matching `[[scope]] match_prefix`."""
    from agent_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("AC_KIT_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    scope_manifest = _write(
        tmp_path / "dotfiles" / "personal.toml",
        """
        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )
    config_dir = tmp_path / ".config" / "agent-config-kit"
    _write(
        config_dir / "config.toml",
        f"""
        [[scope]]
        match_prefix = "{tmp_path / "code" / "personal"}"
        manifest = "{scope_manifest}"
        write_scope = "project"
        """,
    )

    repo = tmp_path / "code" / "personal" / "myrepo"
    _init_git_repo(repo)
    _add_remote(repo, "origin", "https://gitlab.com/someone/myrepo.git")
    monkeypatch.chdir(repo)

    _run_ok(app, ["apply", "--platform", "claude"])

    out = capsys.readouterr().out
    assert "resolved manifest from scope prefix" in out
    assert (repo / ".mcp.json").is_file()


def test_zero_arg_apply_falls_back_to_default_manifest(tmp_path, monkeypatch, capsys):
    from agent_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("AC_KIT_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    default_manifest = _write(
        tmp_path / "dotfiles" / "agent-config.toml",
        """
        [options]
        scope = "project"

        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )
    config_dir = tmp_path / ".config" / "agent-config-kit"
    _write(config_dir / "config.toml", f'default_manifest = "{default_manifest}"\n')

    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    _run_ok(app, ["apply", "--platform", "claude"])

    out = capsys.readouterr().out
    assert "resolved manifest from default_manifest" in out
    assert (cwd / ".mcp.json").is_file()


def test_per_profile_include_end_to_end_through_cli(tmp_path, monkeypatch):
    from agent_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / "skills" / "webapp-testing").mkdir(parents=True)
    (tmp_path / "skills" / "webapp-testing" / "SKILL.md").write_text("# webapp-testing")
    _write(
        tmp_path / "bundles" / "frontend.toml",
        """
        [skills]
        webapp-testing = "../skills/webapp-testing/SKILL.md"
        """,
    )
    repo = tmp_path / "repo"
    _write(
        repo / "agent-config.toml",
        """
        [profiles.frontend]
        include = ["../bundles/frontend.toml"]
        """,
    )
    monkeypatch.chdir(repo)

    _run_ok(
        app,
        [
            "apply",
            str(repo / "agent-config.toml"),
            "--platform",
            "claude",
            "--scope",
            "project",
            "--profile",
            "frontend",
        ],
    )

    assert (repo / ".claude" / "skills" / "webapp-testing" / "SKILL.md").is_file()


def test_prune_state_is_not_shared_across_repos_applying_the_same_org_manifest(
    tmp_path, monkeypatch
):
    """O-STATE (spec §9): two different repos resolving the SAME shared
    [[org]] manifest with project write-scope must not clobber each
    other's `--prune` state — each repo tracks (and prunes) only what it
    wrote to itself."""
    from agent_kit.cli import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("AC_KIT_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    org_manifest = _write(
        tmp_path / "org" / "agent-config.toml",
        """
        [options]
        scope = "project"

        [mcp_servers.witan]
        kind = "stdio"
        command = "uvx"
        """,
    )
    config_dir = tmp_path / ".config" / "agent-config-kit"
    _write(
        config_dir / "config.toml",
        f"""
        [[org]]
        name = "mitodl"
        manifest = "{org_manifest}"
        """,
    )

    repo_a = tmp_path / "code" / "repo-a"
    _init_git_repo(repo_a)
    _add_remote(repo_a, "origin", "https://github.com/mitodl/repo-a.git")
    repo_b = tmp_path / "code" / "repo-b"
    _init_git_repo(repo_b)
    _add_remote(repo_b, "origin", "https://github.com/mitodl/repo-b.git")

    monkeypatch.chdir(repo_a)
    _run_ok(app, ["apply", "--platform", "claude", "--prune"])
    monkeypatch.chdir(repo_b)
    _run_ok(app, ["apply", "--platform", "claude", "--prune"])

    state_a = repo_a / ".agent-config-kit-state.json"
    state_b = repo_b / ".agent-config-kit-state.json"
    assert state_a.is_file()
    assert state_b.is_file()
    assert not (tmp_path / "org" / "agent-config.toml.lock.json").is_file()

    # Both repos still have their own independently-written entry.
    assert json.loads((repo_a / ".mcp.json").read_text())["mcpServers"]["witan"]
    assert json.loads((repo_b / ".mcp.json").read_text())["mcpServers"]["witan"]

    # Drop the server from the shared manifest and re-prune repo_a only --
    # repo_b's entry (and its own state) must be untouched.
    org_manifest.write_text(
        """
        [options]
        scope = "project"
        """
    )
    monkeypatch.chdir(repo_a)
    _run_ok(app, ["apply", "--platform", "claude", "--prune"])

    assert "witan" not in json.loads((repo_a / ".mcp.json").read_text())["mcpServers"]
    assert json.loads((repo_b / ".mcp.json").read_text())["mcpServers"]["witan"]
