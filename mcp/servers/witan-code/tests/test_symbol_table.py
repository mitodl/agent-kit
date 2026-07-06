"""Tests for the per-repo symbol table (Stage 1 — docs/SYMBOL_TABLE.md).

Unit tests cover parse_symbol round-trips and the _symbol_table_records
aggregation; integration tests verify RepoSymbol rows land in the bridge store
and stay exact across incremental reindexes.
"""

from witan_code.bridge import _symbol_table_records
from witan_code.bridge_extractors import canonical_symbol, parse_symbol
from witan_code.package_map import PackageIdentity

from .conftest import requires_stack

REPO = "https://github.com/test/repo-a"

IDENTITY = PackageIdentity(
    name="mit-learn", manager="pypi", version="main", provides=(), declared=True
)


def _b(kind, key, role, file="src/x.ts", line=1, confidence=1.0, **kw):
    from witan_code.bridge_extractors import _binding

    binding = _binding(kind, key, role, file, line=line, **kw)
    binding.confidence = confidence
    return binding


def _with_symbol(binding, identity=None):
    binding.symbol = canonical_symbol(binding, identity)
    return binding


# ── parse_symbol ──────────────────────────────────────────────────


def test_parse_symbol_round_trips_provider_endpoint():
    b = _with_symbol(
        _b("endpoint", "GET /api/v0/users/me", "provider", "openapi.json"), IDENTITY
    )
    parsed = parse_symbol(b.symbol)
    assert parsed.scheme == "http"
    assert parsed.manager == "pypi"
    assert parsed.package == "mit-learn"
    assert parsed.version == "main"
    assert parsed.descriptor == "GET /api/v0/users/me"


def test_parse_symbol_round_trips_unresolved_consumer():
    b = _with_symbol(_b("endpoint", "/api/v0/users/me", "consumer"))
    parsed = parse_symbol(b.symbol)
    assert parsed.scheme == "http"
    assert (parsed.manager, parsed.package, parsed.version) == (".", ".", ".")
    assert parsed.descriptor == "* /api/v0/users/me"


def test_parse_symbol_descriptor_keeps_colons():
    parsed = parse_symbol("svc:.:ol-infrastructure:main:repo/https://github.com/x/y")
    assert parsed.descriptor == "repo/https://github.com/x/y"


def test_parse_symbol_unescapes_fields():
    identity = PackageIdentity(
        name="odd:name%x", manager="pypi", version="main", provides=(), declared=True
    )
    b = _with_symbol(_b("env_var", "FOO_BAR", "provider", "Pulumi.QA.yaml"), identity)
    parsed = parse_symbol(b.symbol)
    assert parsed.package == "odd:name%x"


def test_parse_symbol_rejects_malformed():
    assert parse_symbol("http:pypi:short") is None


# ── _symbol_table_records aggregation ─────────────────────────────


def test_aggregation_dedupes_and_counts_refs():
    b1 = _with_symbol(_b("env_var", "FOO", "consumer", "a.py", line=3))
    b2 = _with_symbol(_b("env_var", "FOO", "consumer", "b.py", line=9))
    records = _symbol_table_records(REPO, [], [b1, b2])
    assert len(records) == 1
    data = records[0]["data"]
    assert data["role"] == "external"
    assert data["n_refs"] == 2
    assert data["scheme"] == "env"
    assert data["descriptor"] == "FOO"
    assert data["key_norm"] == "FOO"
    # Deterministic exemplar: lexicographic minimum.
    assert (data["file"], data["line"]) == ("a.py", 3)
    assert data["slug"] == f"{REPO}|external|{b1.symbol}"


def test_aggregation_maps_roles_and_keeps_max_confidence():
    prov = _with_symbol(
        _b("endpoint", "GET /api/v1/x/", "provider", "openapi.json"), IDENTITY
    )
    c1 = _with_symbol(_b("endpoint", "/api/v1/x/", "consumer", "a.ts", confidence=0.2))
    c2 = _with_symbol(_b("endpoint", "/api/v1/x/", "consumer", "b.ts", confidence=0.7))
    records = _symbol_table_records(REPO, [], [prov, c1, c2])
    by_role = {r["data"]["role"]: r["data"] for r in records}
    assert set(by_role) == {"exported", "external"}
    assert by_role["exported"]["confidence"] == 1.0
    assert by_role["exported"]["package"] == "mit-learn"
    assert by_role["external"]["confidence"] == 0.7
    assert by_role["external"]["package"] == "."
    # http key_norm is the method-less path — shared by both roles.
    assert by_role["exported"]["key_norm"] == by_role["external"]["key_norm"]


def test_aggregation_merges_surviving_store_rows():
    fresh = _with_symbol(_b("env_var", "FOO", "consumer", "a.py", line=3))
    surviving = [
        {
            "symbol": fresh.symbol,
            "kind": "env_var",
            "role": "consumer",
            "key_norm": "FOO",
            "file": "old.py",
            "line": 7,
            "confidence": 1.0,
        }
    ]
    records = _symbol_table_records(REPO, surviving, [fresh])
    assert records[0]["data"]["n_refs"] == 2


def test_package_descriptor_is_always_dot_key_norm_disambiguates():
    """pkg canonical descriptors collapse to "." regardless of package name —
    only key_norm (which carries the package name) can join package symbols."""
    foo = _with_symbol(
        _b("package", "@mitodl/foo", "consumer", "a.ts", framework="npm")
    )
    bar = _with_symbol(
        _b("package", "@mitodl/bar", "provider", "package.json", framework="npm")
    )
    records = {
        r["data"]["role"]: r["data"]
        for r in _symbol_table_records(REPO, [], [foo, bar])
    }
    assert records["external"]["descriptor"] == records["exported"]["descriptor"] == "."
    assert records["external"]["key_norm"] == "@mitodl/foo"
    assert records["exported"]["key_norm"] == "@mitodl/bar"


def test_aggregation_skips_rows_without_symbol():
    legacy = {"symbol": None, "kind": "env_var", "role": "consumer", "file": "x.py"}
    shared = _b("env_var", "FOO", "shared")
    shared.symbol = "env:.:.:.:FOO"
    assert _symbol_table_records(REPO, [legacy], [shared]) == []


# ── Integration: RepoSymbol rows in the bridge store ──────────────


A_SETTINGS = """\
from main.envs import get_string

APP_BASE_URL = get_string("MITOL_APP_BASE_URL", None)
"""

A_CLIENT_TS = """\
export async function listCourses(id) {
  const path = `/api/v1/courses/${id}/`
  return fetch(path)
}
"""

B_PULUMI = """\
config:
  learn_ai:env_vars:
    MITOL_APP_BASE_URL: "https://learn-ai-qa.ol.mit.edu"
"""

B_OPENAPI = (
    '{"openapi": "3.0.0", "paths": '
    '{"/api/v1/courses/{id}/": {"get": {"operationId": "courses_retrieve"}}}}'
)


def _bridge_client(cfg):
    from witan_code import config as cfg_mod
    from witan_code.graph import OmnigraphClient

    return OmnigraphClient(
        str(cfg_mod.bridge_store_path(cfg.code_dir)), cfg.queries_dir
    )


@requires_stack
def test_symbol_table_written_and_joinable(tmp_path, monkeypatch):
    from witan_code import config as cfg_mod
    from witan_code import indexer

    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))

    a = tmp_path / "repo_a"
    (a / "main").mkdir(parents=True)
    (a / "main" / "settings.py").write_text(A_SETTINGS)
    (a / "client.ts").write_text(A_CLIENT_TS)
    b = tmp_path / "repo_b"
    b.mkdir()
    (b / "Pulumi.QA.yaml").write_text(B_PULUMI)
    (b / "openapi.json").write_text(B_OPENAPI)

    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/repo-a")
    indexer.index_path(a, config=cfg_mod.load())
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/repo-b")
    cfg = cfg_mod.load()
    indexer.index_path(b, config=cfg)

    client = _bridge_client(cfg)

    rows_a = client.read(
        "bridge.gq", "repo_symbols", {"repo": "https://github.com/test/repo-a"}
    )
    assert rows_a and all(r["role"] == "external" for r in rows_a)
    ext = {r["symbol"] for r in rows_a}
    assert "env:.:.:.:MITOL_APP_BASE_URL" in ext
    assert "http:.:.:.:* /api/v1/courses/{}" in ext

    rows_b = client.read(
        "bridge.gq", "repo_symbols", {"repo": "https://github.com/test/repo-b"}
    )
    exported = {r["symbol"] for r in rows_b if r["role"] == "exported"}
    assert any(
        s.startswith("env:") and s.endswith("MITOL_APP_BASE_URL") for s in exported
    )
    assert any(
        s.startswith("http:") and s.endswith("GET /api/v1/courses/{}") for s in exported
    )

    # Stage-2 join primitives find both sides.
    env_rows = client.read(
        "bridge.gq",
        "symbols_by_descriptor",
        {"scheme": "env", "descriptor": "MITOL_APP_BASE_URL"},
    )
    assert {(r["repo"], r["role"]) for r in env_rows} == {
        ("https://github.com/test/repo-a", "external"),
        ("https://github.com/test/repo-b", "exported"),
    }
    http_rows = client.read(
        "bridge.gq",
        "symbols_by_key",
        {"scheme": "http", "key_norm": "/api/v1/courses/{}"},
    )
    assert {(r["repo"], r["role"]) for r in http_rows} == {
        ("https://github.com/test/repo-a", "external"),
        ("https://github.com/test/repo-b", "exported"),
    }


@requires_stack
def test_package_symbols_join_via_key_norm_not_descriptor(tmp_path, monkeypatch):
    """symbols_by_descriptor(scheme="pkg", descriptor=".") is unusably broad —
    every package symbol shares descriptor "." — so Stage 2 must join packages
    via symbols_by_key instead, which uses key_norm (the package name)."""
    from witan_code import config as cfg_mod
    from witan_code import indexer

    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))

    consumer = tmp_path / "consumer"
    consumer.mkdir()
    (consumer / "client.ts").write_text(
        "import { search } from '@mitodl/course-search-utils';\n"
    )
    provider = tmp_path / "provider"
    provider.mkdir()
    (provider / "package.json").write_text('{"name": "@mitodl/course-search-utils"}')
    other_provider = tmp_path / "other"
    other_provider.mkdir()
    (other_provider / "package.json").write_text('{"name": "@mitodl/unrelated"}')

    for repo, path in (
        ("https://github.com/test/consumer", consumer),
        ("https://github.com/test/provider", provider),
        ("https://github.com/test/other", other_provider),
    ):
        monkeypatch.setenv("WITAN_REPO", repo)
        cfg = cfg_mod.load()
        indexer.index_path(path, config=cfg)

    client = _bridge_client(cfg)

    # Over-broad: every pkg symbol (any repo, any package) shares descriptor ".".
    broad = client.read(
        "bridge.gq", "symbols_by_descriptor", {"scheme": "pkg", "descriptor": "."}
    )
    assert {r["repo"] for r in broad} >= {
        "https://github.com/test/consumer",
        "https://github.com/test/provider",
        "https://github.com/test/other",
    }

    # Correct: key_norm carries the package name, isolating just the real pair.
    precise = client.read(
        "bridge.gq",
        "symbols_by_key",
        {"scheme": "pkg", "key_norm": "@mitodl/course-search-utils"},
    )
    assert {(r["repo"], r["role"]) for r in precise} == {
        ("https://github.com/test/consumer", "external"),
        ("https://github.com/test/provider", "exported"),
    }


@requires_stack
def test_symbol_table_exact_across_incremental_reindex(tmp_path, monkeypatch):
    """A narrow reindex must drop table rows whose occurrences went away and
    keep rows backed by untouched sibling files."""
    from witan_code import config as cfg_mod
    from witan_code import indexer

    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/inc")
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    cfg = cfg_mod.load()

    base = tmp_path / "repo"
    base.mkdir()
    (base / "a.ts").write_text('const a = fetch("/api/v0/aaa/");\n')
    (base / "b.ts").write_text('const b = fetch("/api/v0/bbb/");\n')
    indexer.index_path(base, config=cfg)

    def external_paths():
        rows = _bridge_client(cfg).read(
            "bridge.gq", "repo_symbols", {"repo": "https://github.com/test/inc"}
        )
        return {r["key_norm"] for r in rows if r["scheme"] == "http"}

    assert external_paths() == {"/api/v0/aaa", "/api/v0/bbb"}

    # Rewrite a.ts to reference a different endpoint; b.ts untouched.
    (base / "a.ts").write_text('const a = fetch("/api/v0/aaa2/");\n')
    indexer.index_path(base, config=cfg)
    assert external_paths() == {"/api/v0/aaa2", "/api/v0/bbb"}

    # Deleting b.ts clears its symbol on the next full-repo index.
    (base / "b.ts").unlink()
    indexer.index_path(base, config=cfg)
    assert external_paths() == {"/api/v0/aaa2"}
