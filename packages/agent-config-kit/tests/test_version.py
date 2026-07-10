import shutil
import subprocess
from importlib import metadata
from pathlib import Path
from urllib.request import pathname2url

import pytest

from agent_config_kit import version as version_module
from agent_config_kit.version import resolve_version

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary not on PATH"
)


class _FakeDistribution:
    def __init__(self, version: str, direct_url_text: str | None):
        self.version = version
        self._direct_url_text = direct_url_text

    def read_text(self, filename: str) -> str | None:
        assert filename == "direct_url.json"
        return self._direct_url_text


def _patch_distribution(monkeypatch, dist: _FakeDistribution | None):
    def fake_distribution(name):
        if dist is None:
            raise metadata.PackageNotFoundError(name)
        return dist

    monkeypatch.setattr(version_module.metadata, "distribution", fake_distribution)


def _init_git_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "--quiet", "--allow-empty", "-m", "init"],
        cwd=path,
        check=True,
    )
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_resolve_version_unknown_package_returns_unknown(monkeypatch):
    _patch_distribution(monkeypatch, None)

    assert resolve_version("does-not-exist") == "unknown"


def test_resolve_version_plain_install_no_direct_url(monkeypatch):
    _patch_distribution(monkeypatch, _FakeDistribution("1.2.3", None))

    assert resolve_version("some-pkg") == "1.2.3"


def test_resolve_version_malformed_direct_url_json(monkeypatch):
    _patch_distribution(monkeypatch, _FakeDistribution("1.2.3", "{not json"))

    assert resolve_version("some-pkg") == "1.2.3"


def test_resolve_version_vcs_git_install_appends_short_commit(monkeypatch):
    direct_url = (
        '{"url": "https://github.com/mitodl/agent-kit",'
        ' "vcs_info": {"vcs": "git", "commit_id": "abcdef1234567890"}}'
    )
    _patch_distribution(monkeypatch, _FakeDistribution("1.2.3", direct_url))

    assert resolve_version("some-pkg") == "1.2.3 (abcdef1)"


def test_resolve_version_vcs_git_without_commit_id_returns_plain(monkeypatch):
    direct_url = (
        '{"url": "https://github.com/mitodl/agent-kit", "vcs_info": {"vcs": "git"}}'
    )
    _patch_distribution(monkeypatch, _FakeDistribution("1.2.3", direct_url))

    assert resolve_version("some-pkg") == "1.2.3"


@requires_git
def test_resolve_version_editable_install_appends_git_short_ref(monkeypatch, tmp_path):
    repo = tmp_path / "workspace"
    short_ref = _init_git_repo(repo)
    direct_url = f'{{"url": "file://{repo}", "dir_info": {{"editable": true}}}}'
    _patch_distribution(monkeypatch, _FakeDistribution("1.2.3", direct_url))

    assert resolve_version("some-pkg") == f"1.2.3 ({short_ref})"


@requires_git
def test_resolve_version_editable_install_decodes_percent_encoded_path(
    monkeypatch, tmp_path
):
    repo = tmp_path / "my project"
    short_ref = _init_git_repo(repo)
    url = "file://" + pathname2url(str(repo))
    direct_url = f'{{"url": "{url}", "dir_info": {{"editable": true}}}}'
    _patch_distribution(monkeypatch, _FakeDistribution("1.2.3", direct_url))

    assert resolve_version("some-pkg") == f"1.2.3 ({short_ref})"


def test_resolve_version_editable_missing_url_does_not_use_cwd(monkeypatch, tmp_path):
    # Regression: an empty/missing `url` must never fall through to running
    # git in the process's current working directory.
    direct_url = '{"dir_info": {"editable": true}}'
    _patch_distribution(monkeypatch, _FakeDistribution("1.2.3", direct_url))
    calls = []
    monkeypatch.setattr(
        version_module, "_git_short_ref", lambda path: calls.append(path) or "deadbeef"
    )

    assert resolve_version("some-pkg") == "1.2.3"
    assert calls == []


def test_resolve_version_editable_install_without_git_repo_returns_plain(
    monkeypatch, tmp_path
):
    not_a_repo = tmp_path / "no-git-here"
    not_a_repo.mkdir()
    direct_url = f'{{"url": "file://{not_a_repo}", "dir_info": {{"editable": true}}}}'
    _patch_distribution(monkeypatch, _FakeDistribution("1.2.3", direct_url))

    assert resolve_version("some-pkg") == "1.2.3"


def test_git_short_ref_returns_none_on_timeout(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=2)

    monkeypatch.setattr(version_module.subprocess, "run", fake_run)

    assert version_module._git_short_ref(tmp_path) is None


def test_git_short_ref_returns_none_on_oserror(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        raise OSError("git not found")

    monkeypatch.setattr(version_module.subprocess, "run", fake_run)

    assert version_module._git_short_ref(tmp_path) is None
