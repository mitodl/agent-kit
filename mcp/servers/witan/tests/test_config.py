"""Unit tests for witan.config — no omnigraph binary required."""

import textwrap

import pytest

from witan.config import _match_target, _parse_targets, _to_list, load, _Target


# ── _to_list ──────────────────────────────────────────────────────────────────


def test_to_list_none():
    assert _to_list(None) == []


def test_to_list_string():
    assert _to_list("mitodl") == ["mitodl"]


def test_to_list_list():
    assert _to_list(["a", "b"]) == ["a", "b"]


def test_to_list_coerces_non_string_items():
    assert _to_list([1, 2]) == ["1", "2"]


def test_to_list_invalid():
    with pytest.raises(ValueError, match="Expected a list or string"):
        _to_list(42)


# ── _parse_targets ────────────────────────────────────────────────────────────


def test_parse_targets_empty():
    assert _parse_targets({}) == []


def test_parse_targets_basic():
    raw = {
        "targets": {
            "work": {
                "server": "http://work:8080",
                "match_orgs": ["mitodl"],
            }
        }
    }
    result = _parse_targets(raw)
    assert len(result) == 1
    assert result[0].name == "work"
    assert result[0].server == "http://work:8080"
    assert result[0].match_orgs == ["mitodl"]
    assert result[0].match_repos == []
    assert result[0].match_hosts == []


def test_parse_targets_bare_string_match_orgs():
    """A bare string for match_orgs is normalised to a single-element list."""
    raw = {"targets": {"work": {"match_orgs": "mitodl"}}}
    result = _parse_targets(raw)
    assert result[0].match_orgs == ["mitodl"]


def test_parse_targets_targets_not_a_table():
    with pytest.raises(ValueError, match="must be a table"):
        _parse_targets({"targets": ["not", "a", "table"]})


def test_parse_targets_target_entry_not_a_table():
    with pytest.raises(ValueError, match="must be a table"):
        _parse_targets({"targets": {"work": "not-a-table"}})


# ── _match_target ─────────────────────────────────────────────────────────────

_WORK = _Target(
    name="work",
    server="http://work:8080",
    token=None,
    author=None,
    agent=None,
    model=None,
    match_orgs=["mitodl"],
    match_repos=[],
    match_hosts=[],
)
_PERSONAL = _Target(
    name="personal",
    server=None,
    token=None,
    author=None,
    agent=None,
    model=None,
    match_orgs=["alice"],
    match_repos=["github.com/alice/dotfiles"],
    match_hosts=[],
)
_ENTERPRISE = _Target(
    name="enterprise",
    server=None,
    token=None,
    author=None,
    agent=None,
    model=None,
    match_orgs=[],
    match_repos=[],
    match_hosts=["github.mit.edu"],
)

_TARGETS = [_WORK, _PERSONAL, _ENTERPRISE]


def test_match_by_org():
    t = _match_target(_TARGETS, "https://github.com/mitodl/agent-kit")
    assert t is _WORK


def test_match_by_org_different_host():
    t = _match_target(_TARGETS, "https://gitlab.com/mitodl/some-repo")
    assert t is _WORK


def test_match_by_repo_exact():
    t = _match_target(_TARGETS, "https://github.com/alice/dotfiles")
    assert t is _PERSONAL


def test_match_by_repo_wins_over_org():
    """match_repos takes priority over match_orgs for the same target (and others)."""
    t = _match_target([_PERSONAL, _WORK], "https://github.com/alice/dotfiles")
    assert t is _PERSONAL


def test_match_by_host():
    t = _match_target(_TARGETS, "https://github.mit.edu/some-org/some-repo")
    assert t is _ENTERPRISE


def test_match_host_beats_org():
    """match_hosts is evaluated before match_orgs."""
    org_target = _Target(
        name="org",
        server=None,
        token=None,
        author=None,
        agent=None,
        model=None,
        match_orgs=["some-org"],
        match_repos=[],
        match_hosts=[],
    )
    host_target = _Target(
        name="host",
        server=None,
        token=None,
        author=None,
        agent=None,
        model=None,
        match_orgs=[],
        match_repos=[],
        match_hosts=["github.mit.edu"],
    )
    t = _match_target([org_target, host_target], "https://github.mit.edu/some-org/repo")
    assert t is host_target


def test_match_no_targets():
    assert _match_target([], "https://github.com/mitodl/agent-kit") is None


def test_match_no_match():
    assert _match_target(_TARGETS, "https://github.com/unrelated/repo") is None


def test_match_empty_org_does_not_match(monkeypatch):
    """An empty org segment must not accidentally match a target with '' in match_orgs."""
    bad_target = _Target(
        name="bad",
        server=None,
        token=None,
        author=None,
        agent=None,
        model=None,
        match_orgs=[""],
        match_repos=[],
        match_hosts=[],
    )
    # A URI with no org segment (just a host) should not match
    assert _match_target([bad_target], "https://example.com") is None


# ── load() ────────────────────────────────────────────────────────────────────


@pytest.fixture
def toml_file(tmp_path):
    """Write a config.toml and return its path."""

    def _write(content: str) -> str:
        p = tmp_path / "config.toml"
        p.write_text(textwrap.dedent(content))
        return str(p)

    return _write


def test_load_defaults(monkeypatch, toml_file):
    monkeypatch.setenv("WITAN_CONFIG", toml_file(""))
    monkeypatch.delenv("WITAN_MEMORY_URI", raising=False)
    monkeypatch.delenv("WITAN_AGENT", raising=False)
    monkeypatch.delenv("WITAN_MODEL", raising=False)
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.setenv("WITAN_REPO", "https://github.com/nobody/nothing")

    cfg = load()
    assert cfg.agent == "claude"
    assert cfg.model is None
    assert cfg.target_name is None


def test_load_global_file_values(monkeypatch, toml_file):
    monkeypatch.setenv(
        "WITAN_CONFIG",
        toml_file(
            """
            agent = "pi"
            model = "claude-opus-4-8"
            author = "Alice"
            """
        ),
    )
    monkeypatch.delenv("WITAN_AGENT", raising=False)
    monkeypatch.delenv("WITAN_MODEL", raising=False)
    monkeypatch.setenv("WITAN_REPO", "https://github.com/nobody/nothing")

    cfg = load()
    assert cfg.agent == "pi"
    assert cfg.model == "claude-opus-4-8"
    assert cfg.author == "Alice"
    assert cfg.target_name is None


def test_load_env_overrides_file(monkeypatch, toml_file):
    monkeypatch.setenv("WITAN_CONFIG", toml_file('agent = "pi"\nmodel = "haiku"'))
    monkeypatch.setenv("WITAN_AGENT", "opencode")
    monkeypatch.setenv("WITAN_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("WITAN_REPO", "https://github.com/nobody/nothing")

    cfg = load()
    assert cfg.agent == "opencode"
    assert cfg.model == "claude-sonnet-4-6"


def test_load_auto_detects_target_by_org(monkeypatch, toml_file):
    monkeypatch.setenv(
        "WITAN_CONFIG",
        toml_file(
            """
            [targets.work]
            server = "http://work:8080"
            agent = "claude"
            match_orgs = ["mitodl"]
            """
        ),
    )
    monkeypatch.setenv("WITAN_REPO", "https://github.com/mitodl/agent-kit")
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.delenv("WITAN_MEMORY_URI", raising=False)

    cfg = load()
    assert cfg.target_name == "work"
    assert cfg.graph_uri == "http://work:8080"


def test_load_explicit_target_arg(monkeypatch, toml_file):
    monkeypatch.setenv(
        "WITAN_CONFIG",
        toml_file(
            """
            [targets.personal]
            server = "~/.local/share/witan-personal/graph.omni"
            agent = "pi"
            match_orgs = ["alice"]
            """
        ),
    )
    monkeypatch.setenv("WITAN_REPO", "https://github.com/mitodl/agent-kit")
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.delenv("WITAN_MEMORY_URI", raising=False)

    cfg = load(target="personal")
    assert cfg.target_name == "personal"
    assert cfg.agent == "pi"


def test_load_witan_target_env(monkeypatch, toml_file):
    monkeypatch.setenv(
        "WITAN_CONFIG",
        toml_file(
            """
            [targets.work]
            server = "http://work:8080"
            match_orgs = ["mitodl"]
            """
        ),
    )
    monkeypatch.setenv("WITAN_TARGET", "work")
    monkeypatch.delenv("WITAN_MEMORY_URI", raising=False)

    cfg = load()
    assert cfg.target_name == "work"


def test_load_target_inherits_global_defaults(monkeypatch, toml_file):
    """A target that omits agent/model falls through to global config values."""
    monkeypatch.setenv(
        "WITAN_CONFIG",
        toml_file(
            """
            agent = "pi"
            model = "claude-opus-4-8"

            [targets.work]
            server = "http://work:8080"
            match_orgs = ["mitodl"]
            """
        ),
    )
    monkeypatch.setenv("WITAN_REPO", "https://github.com/mitodl/agent-kit")
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.delenv("WITAN_AGENT", raising=False)
    monkeypatch.delenv("WITAN_MODEL", raising=False)
    monkeypatch.delenv("WITAN_MEMORY_URI", raising=False)

    cfg = load()
    assert cfg.target_name == "work"
    assert cfg.agent == "pi"
    assert cfg.model == "claude-opus-4-8"


def test_load_unknown_explicit_target_raises(monkeypatch, toml_file):
    monkeypatch.setenv("WITAN_CONFIG", toml_file('[targets.work]\nmatch_orgs = ["x"]'))
    monkeypatch.delenv("WITAN_TARGET", raising=False)

    with pytest.raises(ValueError, match="Unknown target 'nope'"):
        load(target="nope")


def test_load_missing_config_file_uses_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("WITAN_CONFIG", str(tmp_path / "nonexistent.toml"))
    monkeypatch.delenv("WITAN_AGENT", raising=False)
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.setenv("WITAN_REPO", "https://github.com/nobody/nothing")

    cfg = load()
    assert cfg.agent == "claude"
    assert cfg.target_name is None


def test_load_malformed_toml_raises(monkeypatch, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("this is not [ valid toml !!!")
    monkeypatch.setenv("WITAN_CONFIG", str(bad))

    with pytest.raises(ValueError, match="Failed to parse config file"):
        load()
