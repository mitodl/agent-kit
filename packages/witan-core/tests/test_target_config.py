"""Tests for the shared [targets.*] parsing/matching used by witan and witan-code.

The precedence-order and to_list tests formerly lived in witan/tests/test_config.py
(_to_list, _match_target) — they moved here with the logic. Each server's own
test suite keeps only its typed-target/load() integration tests.
"""

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from witan_core.target_config import (
    local_project_path,
    match_target,
    parse_target_tables,
    to_list,
)


# ── to_list ──────────────────────────────────────────────────────────────────


def test_to_list_none():
    assert to_list(None) == []


def test_to_list_string():
    assert to_list("mitodl") == ["mitodl"]


def test_to_list_list():
    assert to_list(["a", "b"]) == ["a", "b"]


def test_to_list_coerces_non_string_items():
    assert to_list([1, 2]) == ["1", "2"]


def test_to_list_invalid():
    with pytest.raises(ValueError, match="Expected a list or string"):
        to_list(42)


# ── parse_target_tables ──────────────────────────────────────────────────────


def test_parse_target_tables_empty():
    assert parse_target_tables({}) == {}


def test_parse_target_tables_basic():
    raw = {
        "targets": {"work": {"server": "http://work:8080", "match_orgs": ["mitodl"]}}
    }
    result = parse_target_tables(raw)
    assert result == {"work": {"server": "http://work:8080", "match_orgs": ["mitodl"]}}


def test_parse_target_tables_not_a_table():
    with pytest.raises(ValueError, match="must be a table"):
        parse_target_tables({"targets": ["not", "a", "table"]})


def test_parse_target_tables_entry_not_a_table():
    with pytest.raises(ValueError, match="must be a table"):
        parse_target_tables({"targets": {"work": "not-a-table"}})


# ── match_target ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Target:
    """Minimal stand-in for a server's own pydantic target model — only the
    four match_* attributes matter to match_target's structural typing."""

    name: str
    match_orgs: list[str] = field(default_factory=list)
    match_repos: list[str] = field(default_factory=list)
    match_hosts: list[str] = field(default_factory=list)
    match_paths: list[str] = field(default_factory=list)


_WORK = _Target(name="work", match_orgs=["mitodl"])
_PERSONAL = _Target(
    name="personal", match_orgs=["alice"], match_repos=["github.com/alice/dotfiles"]
)
_ENTERPRISE = _Target(name="enterprise", match_hosts=["github.mit.edu"])

_TARGETS = [_WORK, _PERSONAL, _ENTERPRISE]


def test_match_by_org():
    assert (
        match_target(_TARGETS, repo_uri="https://github.com/mitodl/agent-kit") is _WORK
    )


def test_match_by_org_different_host():
    assert (
        match_target(_TARGETS, repo_uri="https://gitlab.com/mitodl/some-repo") is _WORK
    )


def test_match_by_repo_exact():
    t = match_target(_TARGETS, repo_uri="https://github.com/alice/dotfiles")
    assert t is _PERSONAL


def test_match_by_repo_wins_over_org():
    """match_repos takes priority over match_orgs for the same target (and others)."""
    t = match_target([_PERSONAL, _WORK], repo_uri="https://github.com/alice/dotfiles")
    assert t is _PERSONAL


def test_match_by_host():
    t = match_target(_TARGETS, repo_uri="https://github.mit.edu/some-org/some-repo")
    assert t is _ENTERPRISE


def test_match_host_beats_org():
    """match_hosts is evaluated before match_orgs."""
    org_target = _Target(name="org", match_orgs=["some-org"])
    host_target = _Target(name="host", match_hosts=["github.mit.edu"])
    t = match_target(
        [org_target, host_target], repo_uri="https://github.mit.edu/some-org/repo"
    )
    assert t is host_target


def test_match_no_targets():
    assert match_target([], repo_uri="https://github.com/mitodl/agent-kit") is None


def test_match_no_match():
    assert match_target(_TARGETS, repo_uri="https://github.com/unrelated/repo") is None


def test_match_empty_org_does_not_match():
    """An empty org segment must not accidentally match a target with '' in match_orgs."""
    bad_target = _Target(name="bad", match_orgs=[""])
    assert match_target([bad_target], repo_uri="https://example.com") is None


def test_match_no_repo_uri_and_no_local_path_returns_none():
    assert match_target(_TARGETS, repo_uri=None, local_path=None) is None


# ── match_paths ──────────────────────────────────────────────────────────────


def test_match_by_local_path(tmp_path):
    checkout = tmp_path / "work" / "some-repo"
    checkout.mkdir(parents=True)
    t = _Target(name="work", match_paths=[str(tmp_path / "work")])
    assert match_target([t], local_path=checkout) is t


def test_match_by_local_path_exact_dir(tmp_path):
    t = _Target(name="work", match_paths=[str(tmp_path)])
    assert match_target([t], local_path=tmp_path) is t


def test_match_by_local_path_does_not_match_sibling_prefix(tmp_path):
    """ "~/code/work" must not match "~/code/work-other" — prefix match is
    directory-boundary-aware, not a bare string prefix."""
    (tmp_path / "work").mkdir()
    (tmp_path / "work-other").mkdir()
    t = _Target(name="work", match_paths=[str(tmp_path / "work")])
    assert match_target([t], local_path=tmp_path / "work-other") is None


def test_match_path_beats_repo_org_host(tmp_path):
    """match_paths is the most specific tier — it wins even when repo_uri
    would also match a different target via match_orgs."""
    checkout = tmp_path / "personal-checkout"
    checkout.mkdir()
    path_target = _Target(name="path-pin", match_paths=[str(checkout)])
    org_target = _Target(name="org", match_orgs=["mitodl"])
    t = match_target(
        [org_target, path_target],
        repo_uri="https://github.com/mitodl/agent-kit",
        local_path=checkout,
    )
    assert t is path_target


def test_match_path_runs_even_without_repo_uri(tmp_path):
    """A local-only checkout (no git remote) can still route by path alone."""
    checkout = tmp_path / "no-remote"
    checkout.mkdir()
    t = _Target(name="local-only", match_paths=[str(checkout)])
    assert match_target([t], repo_uri=None, local_path=checkout) is t


def test_match_path_expands_tilde(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    checkout = tmp_path / "code" / "personal" / "dotfiles"
    checkout.mkdir(parents=True)
    t = _Target(name="personal", match_paths=["~/code/personal"])
    assert match_target([t], local_path=checkout) is t


def test_match_no_path_targets_falls_through_to_repo_tiers(tmp_path):
    t = match_target(
        _TARGETS, repo_uri="https://github.com/mitodl/agent-kit", local_path=tmp_path
    )
    assert t is _WORK


# ── local_project_path ────────────────────────────────────────────────────────


def test_local_project_path_defaults_to_cwd(monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    assert local_project_path() == Path.cwd()


def test_local_project_path_prefers_claude_project_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    assert local_project_path() == tmp_path
