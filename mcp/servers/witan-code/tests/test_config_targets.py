"""Tests for witan_code.config's [targets.<name>] routing (load(), _parse_targets).

The match-precedence logic itself (match_paths/match_repos/match_hosts/
match_orgs) lives in witan_core.target_config, with its own test suite at
packages/witan-core/tests/test_target_config.py; this file covers only
witan-code's own typed target model and load() integration.
"""

import textwrap

import pytest

from witan_code.config import _parse_targets, load, load_remote_config


# ── _parse_targets ────────────────────────────────────────────────────────────


def test_parse_targets_empty():
    assert _parse_targets({}) == []


def test_parse_targets_basic():
    raw = {
        "targets": {"work": {"code_dir": "/mnt/work-code", "match_orgs": ["mitodl"]}}
    }
    result = _parse_targets(raw)
    assert len(result) == 1
    assert result[0].name == "work"
    assert result[0].code_dir == "/mnt/work-code"
    assert result[0].match_orgs == ["mitodl"]
    assert result[0].match_paths == []


def test_parse_targets_targets_not_a_table():
    with pytest.raises(ValueError, match="must be a table"):
        _parse_targets({"targets": ["not", "a", "table"]})


def test_parse_targets_target_entry_not_a_table():
    with pytest.raises(ValueError, match="must be a table"):
        _parse_targets({"targets": {"work": "not-a-table"}})


def test_parse_targets_invalid_match_orgs_type():
    raw = {"targets": {"work": {"match_orgs": 42}}}
    with pytest.raises(ValueError, match="Expected a list or string"):
        _parse_targets(raw)


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
    monkeypatch.delenv("WITAN_CODE_DIR", raising=False)
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.setenv("WITAN_REPO", "https://github.com/nobody/nothing")

    cfg = load()
    assert cfg.target_name is None
    assert str(cfg.code_dir).endswith("/.local/share/witan/code")


def test_load_env_overrides_file(monkeypatch, toml_file):
    monkeypatch.setenv("WITAN_CONFIG", toml_file('code_dir = "/from-file"'))
    monkeypatch.setenv("WITAN_CODE_DIR", "/from-env")
    monkeypatch.setenv("WITAN_REPO", "https://github.com/nobody/nothing")

    cfg = load()
    assert str(cfg.code_dir) == "/from-env"


def test_load_global_file_code_dir(monkeypatch, toml_file):
    monkeypatch.setenv("WITAN_CONFIG", toml_file('code_dir = "/from-file"'))
    monkeypatch.delenv("WITAN_CODE_DIR", raising=False)
    monkeypatch.setenv("WITAN_REPO", "https://github.com/nobody/nothing")

    cfg = load()
    assert str(cfg.code_dir) == "/from-file"


def test_load_auto_detects_target_by_org(monkeypatch, toml_file):
    monkeypatch.setenv(
        "WITAN_CONFIG",
        toml_file(
            """
            [targets.work]
            code_dir = "/work-code"
            match_orgs = ["mitodl"]
            """
        ),
    )
    monkeypatch.setenv("WITAN_REPO", "https://github.com/mitodl/agent-kit")
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.delenv("WITAN_CODE_DIR", raising=False)

    cfg = load()
    assert cfg.target_name == "work"
    assert str(cfg.code_dir) == "/work-code"


def test_load_auto_detects_target_by_path(monkeypatch, toml_file, tmp_path):
    checkout = tmp_path / "code" / "personal" / "dotfiles"
    checkout.mkdir(parents=True)
    monkeypatch.setenv(
        "WITAN_CONFIG",
        toml_file(
            f"""
            [targets.personal]
            code_dir = "/personal-code"
            match_paths = [{str(tmp_path / "code" / "personal")!r}]
            """
        ),
    )
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(checkout))
    monkeypatch.setenv("WITAN_REPO", "")
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.delenv("WITAN_CODE_DIR", raising=False)

    cfg = load()
    assert cfg.target_name == "personal"
    assert str(cfg.code_dir) == "/personal-code"


def test_load_explicit_target_arg(monkeypatch, toml_file):
    monkeypatch.setenv(
        "WITAN_CONFIG",
        toml_file(
            """
            [targets.personal]
            code_dir = "/personal-code"
            match_orgs = ["alice"]
            """
        ),
    )
    monkeypatch.setenv("WITAN_REPO", "https://github.com/mitodl/agent-kit")
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.delenv("WITAN_CODE_DIR", raising=False)

    cfg = load(target="personal")
    assert cfg.target_name == "personal"
    assert str(cfg.code_dir) == "/personal-code"


def test_load_witan_target_env(monkeypatch, toml_file):
    monkeypatch.setenv(
        "WITAN_CONFIG",
        toml_file(
            """
            [targets.work]
            code_dir = "/work-code"
            match_orgs = ["mitodl"]
            """
        ),
    )
    monkeypatch.setenv("WITAN_TARGET", "work")
    monkeypatch.delenv("WITAN_CODE_DIR", raising=False)

    cfg = load()
    assert cfg.target_name == "work"


def test_load_target_inherits_global_defaults(monkeypatch, toml_file):
    """A target that omits author falls through to the global config value."""
    monkeypatch.setenv(
        "WITAN_CONFIG",
        toml_file(
            """
            author = "Alice"

            [targets.work]
            code_dir = "/work-code"
            match_orgs = ["mitodl"]
            """
        ),
    )
    monkeypatch.setenv("WITAN_REPO", "https://github.com/mitodl/agent-kit")
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.delenv("WITAN_AUTHOR", raising=False)
    monkeypatch.delenv("WITAN_CODE_DIR", raising=False)

    cfg = load()
    assert cfg.target_name == "work"
    assert cfg.author == "Alice"


def test_load_unknown_explicit_target_raises(monkeypatch, toml_file):
    monkeypatch.setenv("WITAN_CONFIG", toml_file('[targets.work]\nmatch_orgs = ["x"]'))
    monkeypatch.delenv("WITAN_TARGET", raising=False)

    with pytest.raises(ValueError, match="Unknown target 'nope'"):
        load(target="nope")


def test_load_missing_config_file_uses_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("WITAN_CONFIG", str(tmp_path / "nonexistent.toml"))
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.setenv("WITAN_REPO", "https://github.com/nobody/nothing")

    cfg = load()
    assert cfg.target_name is None


def test_load_malformed_toml_raises(monkeypatch, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("this is not [ valid toml !!!")
    monkeypatch.setenv("WITAN_CONFIG", str(bad))

    with pytest.raises(ValueError, match="Failed to parse config file"):
        load()


def test_load_shares_config_file_with_witan(monkeypatch, toml_file):
    """One [targets.<name>] block can carry both witan's server/graph and
    witan-code's code_dir — this server only reads the fields it knows."""
    monkeypatch.setenv(
        "WITAN_CONFIG",
        toml_file(
            """
            [targets.work]
            server = "http://work:8080"
            graph = "council-work"
            code_dir = "/work-code"
            match_orgs = ["mitodl"]
            """
        ),
    )
    monkeypatch.setenv("WITAN_REPO", "https://github.com/mitodl/agent-kit")
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.delenv("WITAN_CODE_DIR", raising=False)

    cfg = load()
    assert cfg.target_name == "work"
    assert str(cfg.code_dir) == "/work-code"


# ── load_remote_config (ADR 0005) ─────────────────────────────────────────────
# The resolution order itself is shared with witan (witan-council) and covered
# there and in witan_core; these pin that witan-code reads the SAME keys off the
# SAME target, which is what makes one deployment serve both CLIs.


def _isolate_remote(monkeypatch, tmp_path):
    monkeypatch.setenv("WITAN_CONFIG", str(tmp_path / "unused.toml"))
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    for var in ("WITAN_REMOTE_URL", "WITAN_OIDC_ISSUER", "WITAN_OIDC_CLIENT_ID"):
        monkeypatch.delenv(var, raising=False)


def test_load_remote_config_unset_is_none(monkeypatch, tmp_path):
    _isolate_remote(monkeypatch, tmp_path)
    assert load_remote_config() is None


def test_load_remote_config_from_env(monkeypatch, tmp_path):
    _isolate_remote(monkeypatch, tmp_path)
    monkeypatch.setenv("WITAN_REMOTE_URL", "https://witan.example.org/mcp")
    monkeypatch.setenv("WITAN_OIDC_ISSUER", "https://sso.example.org/realms/ol")

    cfg = load_remote_config()
    assert cfg.url == "https://witan.example.org/mcp"
    # Shared default: the token cache is keyed by (issuer, client_id), so this
    # matching witan's default is what makes one `witan login` cover both.
    assert cfg.oidc_client_id == "witan-cli"
    assert cfg.target_name is None


def test_load_remote_config_url_without_issuer_raises(monkeypatch, tmp_path):
    _isolate_remote(monkeypatch, tmp_path)
    monkeypatch.setenv("WITAN_REMOTE_URL", "https://witan.example.org/mcp")

    with pytest.raises(ValueError, match="WITAN_OIDC_ISSUER"):
        load_remote_config()


def test_load_remote_config_from_target(monkeypatch, toml_file, tmp_path):
    """The same target block routes code_dir AND the deployed endpoint."""
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.delenv("WITAN_REMOTE_URL", raising=False)
    monkeypatch.delenv("WITAN_OIDC_ISSUER", raising=False)
    monkeypatch.setenv(
        "WITAN_CONFIG",
        toml_file(
            """
            [targets.hosted]
            code_dir = "/work-code"
            remote_url = "https://witan.example.org/mcp"
            oidc_issuer = "https://sso.example.org/realms/ol"
            oidc_audience = "witan"
            match_orgs = ["mitodl"]
            """
        ),
    )
    monkeypatch.setenv("WITAN_REPO", "https://github.com/mitodl/agent-kit")

    cfg = load_remote_config()
    assert cfg.url == "https://witan.example.org/mcp"
    assert cfg.oidc_audience == "witan"
    assert cfg.target_name == "hosted"
