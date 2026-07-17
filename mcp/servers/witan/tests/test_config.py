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


def test_parse_targets_invalid_match_orgs_type():
    """An invalid match_orgs value surfaces through pydantic field validation."""
    raw = {"targets": {"work": {"match_orgs": 42}}}
    with pytest.raises(ValueError, match="Expected a list or string"):
        _parse_targets(raw)


def test_target_is_immutable():
    t = _parse_targets({"targets": {"work": {"match_orgs": ["mitodl"]}}})[0]
    with pytest.raises(ValueError):
        t.name = "other"


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
    monkeypatch.delenv("WITAN_MEMORY_GRAPH", raising=False)
    monkeypatch.setenv("WITAN_REPO", "https://github.com/nobody/nothing")

    cfg = load()
    assert cfg.agent == "claude"
    assert cfg.model is None
    assert cfg.target_name is None
    assert cfg.graph_name == "council"


def test_load_graph_name_env_and_target_override(monkeypatch, toml_file):
    monkeypatch.setenv(
        "WITAN_CONFIG",
        toml_file(
            """
            [targets.work]
            server = "http://work:8080"
            graph = "council-work"
            match_orgs = ["mitodl"]
            """
        ),
    )
    monkeypatch.setenv("WITAN_REPO", "https://github.com/mitodl/agent-kit")
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.delenv("WITAN_MEMORY_GRAPH", raising=False)

    # target's `graph` wins when no env override is set
    assert load().graph_name == "council-work"

    # WITAN_MEMORY_GRAPH env overrides the target
    monkeypatch.setenv("WITAN_MEMORY_GRAPH", "council-env")
    assert load().graph_name == "council-env"


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


# ── load_rank_config ──────────────────────────────────────────────────────────


def test_rank_config_defaults(monkeypatch):
    from witan.config import load_rank_config

    for var in (
        "WITAN_RANK_HALFLIFE_DAYS",
        "WITAN_RANK_DEFAULT_CONF",
        "WITAN_CONFIG",
    ):
        monkeypatch.delenv(var, raising=False)
    rc = load_rank_config()
    assert rc.w_bm25 == 1.0
    assert rc.half_life_days == 90.0


def test_rank_config_env_override(monkeypatch):
    from witan.config import load_rank_config

    monkeypatch.delenv("WITAN_CONFIG", raising=False)
    monkeypatch.setenv("WITAN_RANK_W_RECENCY", "0")
    assert load_rank_config().w_recency == 0.0


def test_rank_config_rejects_nonpositive_half_life(monkeypatch):
    from witan.config import load_rank_config

    monkeypatch.delenv("WITAN_CONFIG", raising=False)
    monkeypatch.setenv("WITAN_RANK_HALFLIFE_DAYS", "0")
    with pytest.raises(ValueError, match="half_life_days must be > 0"):
        load_rank_config()


def test_rank_config_rejects_out_of_range_confidence(monkeypatch):
    from witan.config import load_rank_config

    monkeypatch.delenv("WITAN_CONFIG", raising=False)
    monkeypatch.setenv("WITAN_RANK_DEFAULT_CONF", "1.5")
    with pytest.raises(ValueError, match="between 0.0 and 1.0"):
        load_rank_config()


def test_rank_config_non_numeric_names_source(monkeypatch):
    from witan.config import load_rank_config

    monkeypatch.delenv("WITAN_CONFIG", raising=False)
    monkeypatch.setenv("WITAN_RANK_W_BM25", "notanumber")
    with pytest.raises(ValueError, match="WITAN_RANK_W_BM25"):
        load_rank_config()


def test_rank_config_is_frozen():
    from witan.config import RankConfig

    rc = RankConfig()
    with pytest.raises(ValueError):
        rc.w_bm25 = 2.0


def test_rank_config_non_numeric_type_reports_expected_a_number(monkeypatch, toml_file):
    """A non-string, non-numeric TOML value (e.g. a list) must not be reported as
    an out-of-range half_life_days — it's a type error, not a range error."""
    from witan.config import load_rank_config

    monkeypatch.setenv("WITAN_CONFIG", toml_file("[rank]\nhalf_life_days = [1, 2, 3]"))
    monkeypatch.delenv("WITAN_RANK_HALFLIFE_DAYS", raising=False)
    with pytest.raises(ValueError, match="expected a number") as excinfo:
        load_rank_config()
    assert "must be > 0" not in str(excinfo.value)


# ── load_scan_config ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_scan_env(monkeypatch):
    """Scan settings come from a small fixed set of env vars — clear them so a
    real WITAN_SCAN_* in the dev shell can't leak into these tests."""
    from witan.config import _SCAN_FIELDS

    for env_var in _SCAN_FIELDS.values():
        monkeypatch.delenv(env_var, raising=False)


def test_scan_config_defaults(monkeypatch):
    from witan.config import load_scan_config

    monkeypatch.delenv("WITAN_CONFIG", raising=False)
    sc = load_scan_config()
    assert sc.enabled is True
    assert sc.secret_action == "block"
    assert sc.pii_action == "redact"
    assert sc.on_scanner_error == "block"
    assert sc.enabled_detectors == []
    assert sc.plugins == []


def test_scan_config_enabled_from_env(monkeypatch):
    from witan.config import load_scan_config

    monkeypatch.delenv("WITAN_CONFIG", raising=False)
    monkeypatch.setenv("WITAN_SCAN_ENABLED", "true")
    assert load_scan_config().enabled is True


def test_scan_config_disabled_from_env(monkeypatch):
    """Enabled by default (opt-out) — WITAN_SCAN_ENABLED=false must turn it off."""
    from witan.config import load_scan_config

    monkeypatch.delenv("WITAN_CONFIG", raising=False)
    monkeypatch.setenv("WITAN_SCAN_ENABLED", "false")
    assert load_scan_config().enabled is False


def test_scan_config_action_env_override(monkeypatch):
    from witan.config import load_scan_config

    monkeypatch.delenv("WITAN_CONFIG", raising=False)
    monkeypatch.setenv("WITAN_SCAN_PII_ACTION", "block")
    assert load_scan_config().pii_action == "block"


def test_scan_config_env_list_is_comma_split(monkeypatch):
    from witan.config import load_scan_config

    monkeypatch.delenv("WITAN_CONFIG", raising=False)
    monkeypatch.setenv("WITAN_SCAN_DISABLED_DETECTORS", "aws_key, github_token ,")
    assert load_scan_config().disabled_detectors == ["aws_key", "github_token"]


def test_scan_config_toml_list_is_stripped_and_filtered(monkeypatch, toml_file):
    """TOML list items get the same strip/blank-drop as env values so a stray
    space can't become an unimportable plugin path."""
    from witan.config import load_scan_config

    monkeypatch.setenv(
        "WITAN_CONFIG",
        toml_file("[scan]\nplugins = [' acme:Scanner ', '', 'acme:Other']"),
    )
    assert load_scan_config().plugins == ["acme:Scanner", "acme:Other"]


def test_scan_config_toml_list(monkeypatch, toml_file):
    from witan.config import load_scan_config

    monkeypatch.setenv(
        "WITAN_CONFIG",
        toml_file(
            """
            [scan]
            enabled = true
            plugins = ["acme.scanners:BadgeScanner", "acme.scanners:PanScanner"]
            """
        ),
    )
    sc = load_scan_config()
    assert sc.enabled is True
    assert sc.plugins == ["acme.scanners:BadgeScanner", "acme.scanners:PanScanner"]


def test_scan_config_env_overrides_file(monkeypatch, toml_file):
    from witan.config import load_scan_config

    monkeypatch.setenv("WITAN_CONFIG", toml_file("[scan]\nsecret_action = 'warn'"))
    monkeypatch.setenv("WITAN_SCAN_SECRET_ACTION", "redact")
    assert load_scan_config().secret_action == "redact"


def test_scan_config_rejects_unknown_action(monkeypatch):
    from witan.config import load_scan_config

    monkeypatch.delenv("WITAN_CONFIG", raising=False)
    monkeypatch.setenv("WITAN_SCAN_SECRET_ACTION", "nuke")
    with pytest.raises(ValueError, match="WITAN_SCAN_SECRET_ACTION") as excinfo:
        load_scan_config()
    assert "block, redact, warn" in str(excinfo.value)


def test_scan_config_on_error_rejects_redact(monkeypatch):
    """on_scanner_error can't redact (no spans to redact) — only block or warn."""
    from witan.config import load_scan_config

    monkeypatch.delenv("WITAN_CONFIG", raising=False)
    monkeypatch.setenv("WITAN_SCAN_ON_ERROR", "redact")
    with pytest.raises(ValueError, match="expected one of block, warn"):
        load_scan_config()


def test_scan_config_rejects_non_boolean_enabled(monkeypatch):
    from witan.config import load_scan_config

    monkeypatch.delenv("WITAN_CONFIG", raising=False)
    monkeypatch.setenv("WITAN_SCAN_ENABLED", "maybe")
    with pytest.raises(ValueError, match="expected a boolean"):
        load_scan_config()


def test_scan_config_section_not_a_table(monkeypatch, toml_file):
    from witan.config import load_scan_config

    monkeypatch.setenv("WITAN_CONFIG", toml_file("scan = 'on'"))
    with pytest.raises(ValueError, match="'scan' section .* must be a table"):
        load_scan_config()


def test_scan_config_is_frozen():
    from witan.config import ScanConfig

    sc = ScanConfig()
    with pytest.raises(ValueError):
        sc.enabled = True


# ── overlay (per-repo policy, ADR 0001 amendment 2026-07-09) ───────────────────


def test_overlay_has_no_env_var_form(monkeypatch):
    """Deliberate: overlay policy is admin-owned via TOML only — there must be
    no WITAN_SCAN_OVERLAY* env var a client-controlled process could set."""
    from witan.config import _SCAN_FIELDS

    assert not any("OVERLAY" in v for v in _SCAN_FIELDS.values())


def test_for_repo_with_no_overlay_returns_self():
    from witan.config import ScanConfig

    cfg = ScanConfig()
    assert cfg.for_repo("github.com/example/repo") is cfg
    assert cfg.for_repo(None) is cfg


def test_for_repo_applies_matching_overlay():
    from witan.config import ScanConfig

    cfg = ScanConfig(overlay={"github.com/example/repo": {"secret_action": "warn"}})
    effective = cfg.for_repo("github.com/example/repo")
    assert effective.secret_action == "warn"
    assert effective.pii_action == cfg.pii_action  # untouched fields carry over


def test_for_repo_no_match_returns_base_unchanged():
    from witan.config import ScanConfig

    cfg = ScanConfig(overlay={"github.com/example/repo": {"secret_action": "warn"}})
    assert cfg.for_repo("github.com/other/repo") is cfg


def test_load_scan_config_reads_overlay_from_toml(monkeypatch, toml_file):
    from witan.config import load_scan_config

    monkeypatch.setenv(
        "WITAN_CONFIG",
        toml_file(
            """
            [scan.overlay."github.com/example/legacy"]
            secret_action = "warn"
            """
        ),
    )
    cfg = load_scan_config()
    assert cfg.for_repo("github.com/example/legacy").secret_action == "warn"
    assert cfg.for_repo("github.com/other/repo").secret_action == "block"


def test_load_scan_config_overlay_unknown_field_rejected(monkeypatch, toml_file):
    from witan.config import load_scan_config

    monkeypatch.setenv(
        "WITAN_CONFIG",
        toml_file(
            """
            [scan.overlay."github.com/example/legacy"]
            not_a_real_field = "warn"
            """
        ),
    )
    with pytest.raises(ValueError, match="unknown setting"):
        load_scan_config()


def test_load_scan_config_overlay_invalid_value_rejected(monkeypatch, toml_file):
    from witan.config import load_scan_config

    monkeypatch.setenv(
        "WITAN_CONFIG",
        toml_file(
            """
            [scan.overlay."github.com/example/legacy"]
            secret_action = "nuke"
            """
        ),
    )
    with pytest.raises(ValueError, match="is invalid"):
        load_scan_config()


def test_load_scan_config_overlay_not_a_table_rejected(monkeypatch, toml_file):
    from witan.config import load_scan_config

    monkeypatch.setenv("WITAN_CONFIG", toml_file('[scan]\noverlay = "nope"'))
    with pytest.raises(ValueError, match=r"\[scan\.overlay\] must be a table"):
        load_scan_config()


def test_load_scan_config_overlay_entry_not_a_table_rejected(monkeypatch, toml_file):
    from witan.config import load_scan_config

    monkeypatch.setenv(
        "WITAN_CONFIG", toml_file('[scan.overlay]\n"github.com/x/y" = "nope"')
    )
    with pytest.raises(ValueError, match="must be a table"):
        load_scan_config()


# ── overlay repo-key normalization (protocol/case/trailing-slash) ──────────────


def test_for_repo_matches_across_scheme_variants():
    """A TOML key with an explicit scheme must still match a schemeless
    lookup value, and vice versa — the write side almost always carries the
    full `https://` canonical form (witan.repo.detect's output), so failing
    to normalize this would silently disable the overlay entirely."""
    from witan.config import ScanConfig

    cfg = ScanConfig(
        overlay={"https://github.com/example/repo": {"secret_action": "warn"}}
    )
    assert cfg.for_repo("github.com/example/repo").secret_action == "warn"
    assert cfg.for_repo("https://github.com/example/repo").secret_action == "warn"


def test_for_repo_matches_across_case_and_trailing_slash():
    from witan.config import ScanConfig

    cfg = ScanConfig(overlay={"github.com/Example/Repo/": {"secret_action": "warn"}})
    assert cfg.for_repo("github.com/example/repo").secret_action == "warn"
    assert cfg.for_repo("GITHUB.COM/EXAMPLE/REPO").secret_action == "warn"


def test_for_repo_matches_git_suffix():
    from witan.config import ScanConfig

    cfg = ScanConfig(
        overlay={"https://github.com/example/repo.git": {"secret_action": "warn"}}
    )
    assert cfg.for_repo("github.com/example/repo").secret_action == "warn"


def test_overlay_empty_repo_key_rejected():
    from witan.config import ScanConfig

    with pytest.raises(ValueError, match="must not be empty"):
        ScanConfig(overlay={"": {"secret_action": "warn"}})


def test_allowlist_hashes_normalized_to_lowercase():
    from witan.config import ScanConfig

    cfg = ScanConfig(allowlist_hashes=["ABCDEF0123"])
    assert cfg.allowlist_hashes == ["abcdef0123"]


def test_default_config_toml_is_valid_and_fully_commented():
    """Every setting ships commented out — loading it must change nothing."""
    import tomllib

    from witan.config import RankConfig, ScanConfig, default_config_toml

    text = default_config_toml()
    parsed = tomllib.loads(text)
    assert parsed == {"rank": {}, "scan": {}}
    assert RankConfig(**parsed["rank"]) == RankConfig()
    assert ScanConfig(**parsed["scan"]) == ScanConfig()


def test_default_config_toml_reflects_actual_defaults():
    """Commented values must match the real defaults, not stale copy-paste."""
    from witan.config import ScanConfig, default_config_toml

    text = default_config_toml()
    scan = ScanConfig()
    assert f'secret_action = "{scan.secret_action}"' in text
    assert f"enabled = {str(scan.enabled).lower()}" in text


# ── IdentityConfig / load_identity_config (ADR 0004) ─────────────────────────


def test_load_identity_config_defaults_disabled(monkeypatch):
    from witan.config import load_identity_config

    monkeypatch.delenv("WITAN_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("WITAN_OIDC_AUDIENCE", raising=False)
    monkeypatch.delenv("WITAN_ACTOR_TOKENS_FILE", raising=False)
    identity = load_identity_config()
    assert identity.oidc_issuer is None
    assert identity.actor_tokens_file is None


def test_load_identity_config_from_env(monkeypatch):
    from witan.config import load_identity_config

    monkeypatch.setenv("WITAN_OIDC_ISSUER", "https://sso.example.org/realms/witan")
    monkeypatch.setenv("WITAN_OIDC_AUDIENCE", "witan")
    monkeypatch.setenv("WITAN_ACTOR_TOKENS_FILE", "/etc/witan/actor-tokens.json")
    identity = load_identity_config()
    assert identity.oidc_issuer == "https://sso.example.org/realms/witan"
    assert identity.oidc_audience == "witan"
    assert identity.actor_tokens_file == "/etc/witan/actor-tokens.json"


def test_load_identity_config_issuer_without_tokens_file_raises(monkeypatch):
    from witan.config import load_identity_config

    monkeypatch.setenv("WITAN_OIDC_ISSUER", "https://sso.example.org/realms/witan")
    monkeypatch.setenv("WITAN_OIDC_AUDIENCE", "witan")
    monkeypatch.delenv("WITAN_ACTOR_TOKENS_FILE", raising=False)
    with pytest.raises(ValueError, match="must be set together"):
        load_identity_config()


def test_load_identity_config_tokens_file_without_issuer_raises(monkeypatch):
    from witan.config import load_identity_config

    monkeypatch.delenv("WITAN_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("WITAN_OIDC_AUDIENCE", raising=False)
    monkeypatch.setenv("WITAN_ACTOR_TOKENS_FILE", "/etc/witan/actor-tokens.json")
    with pytest.raises(ValueError, match="must be set together"):
        load_identity_config()


def test_load_identity_config_issuer_without_audience_raises(monkeypatch):
    """Audience is not optional: an unchecked aud claim would accept a token
    minted for a different client (token-substitution risk)."""
    from witan.config import load_identity_config

    monkeypatch.setenv("WITAN_OIDC_ISSUER", "https://sso.example.org/realms/witan")
    monkeypatch.delenv("WITAN_OIDC_AUDIENCE", raising=False)
    monkeypatch.setenv("WITAN_ACTOR_TOKENS_FILE", "/etc/witan/actor-tokens.json")
    with pytest.raises(ValueError, match="must be set together"):
        load_identity_config()


# ── load_remote_config (ADR 0005) ────────────────────────────────────────────


def test_load_remote_config_unset_is_none(monkeypatch):
    from witan.config import load_remote_config

    monkeypatch.delenv("WITAN_REMOTE_URL", raising=False)
    assert load_remote_config() is None


def test_load_remote_config_populated(monkeypatch):
    from witan.config import load_remote_config

    monkeypatch.setenv("WITAN_REMOTE_URL", "https://witan.example.org/mcp")
    monkeypatch.setenv("WITAN_OIDC_ISSUER", "https://sso.example.org/realms/ol")
    monkeypatch.setenv("WITAN_OIDC_CLIENT_ID", "my-cli")
    monkeypatch.setenv("WITAN_OIDC_AUDIENCE", "witan")
    cfg = load_remote_config()
    assert cfg is not None
    assert cfg.url == "https://witan.example.org/mcp"
    assert cfg.oidc_client_id == "my-cli"
    assert cfg.oidc_audience == "witan"


def test_load_remote_config_defaults_client_id(monkeypatch):
    from witan.config import load_remote_config

    monkeypatch.setenv("WITAN_REMOTE_URL", "https://witan.example.org/mcp")
    monkeypatch.setenv("WITAN_OIDC_ISSUER", "https://sso.example.org/realms/ol")
    monkeypatch.delenv("WITAN_OIDC_CLIENT_ID", raising=False)
    monkeypatch.delenv("WITAN_OIDC_AUDIENCE", raising=False)
    cfg = load_remote_config()
    assert cfg.oidc_client_id == "witan-cli"
    assert cfg.oidc_audience is None


def test_load_remote_config_url_without_issuer_raises(monkeypatch):
    from witan.config import load_remote_config

    monkeypatch.setenv("WITAN_REMOTE_URL", "https://witan.example.org/mcp")
    monkeypatch.delenv("WITAN_OIDC_ISSUER", raising=False)
    with pytest.raises(ValueError, match="WITAN_OIDC_ISSUER"):
        load_remote_config()
