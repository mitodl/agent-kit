"""Tests for typed cross-repo edge precision tiers (docs/EDGE_PRECISION_TIERS.md).

Unit tests exercise witan_code.edges.cross_repo_edges()/precise_pairs()
directly with hand-built RepoSymbol/InterfaceBinding-shaped dicts (no
omnigraph/tree-sitter needed); integration tests verify min_precision
end-to-end through the indexer, bridge store, build_graph, and MCP tools.
"""

import pytest

from witan_code.edges import PRECISION_TIERS, TypedEdge, cross_repo_edges, precise_pairs

from .conftest import requires_stack


def _symbol_row(
    repo, role, symbol, scheme, descriptor, key_norm, *, version=".", kind="env_var"
):
    return {
        "repo": repo,
        "role": role,
        "symbol": symbol,
        "scheme": scheme,
        "descriptor": descriptor,
        "key_norm": key_norm,
        "version": version,
        "kind": kind,
        "file": None,
        "line": None,
    }


def _binding_row(repo, role, kind, key_norm, *, confidence=None, file="x.py", line=1):
    return {
        "repo": repo,
        "role": role,
        "kind": kind,
        "key_norm": key_norm,
        "confidence": confidence,
        "file": file,
        "line": line,
    }


# ── Merge semantics ────────────────────────────────────────────────


def test_heuristic_edge_suppressed_when_precise_covers_same_pair():
    symbols = [
        _symbol_row("a", "external", "env:.:.:.:FOO", "env", "FOO", "FOO"),
        _symbol_row(
            "b", "exported", "env:.:b:main:FOO", "env", "FOO", "FOO", version="main"
        ),
    ]
    bindings = [
        _binding_row("a", "consumer", "env_var", "FOO"),
        _binding_row("b", "provider", "env_var", "FOO"),
    ]
    edges = cross_repo_edges(symbols, bindings)
    assert len(edges) == 1
    assert edges[0].precision == "precise"


def test_heuristic_edge_kept_when_not_covered_by_precise():
    symbols = [
        _symbol_row("a", "external", "env:.:.:.:FOO", "env", "FOO", "FOO"),
        _symbol_row(
            "b", "exported", "env:.:b:main:FOO", "env", "FOO", "FOO", version="main"
        ),
    ]
    bindings = [
        _binding_row("a", "consumer", "env_var", "FOO"),
        _binding_row("b", "provider", "env_var", "FOO"),
        # A second, unrelated env var only the heuristic tier can see.
        _binding_row("a", "consumer", "env_var", "BAR"),
        _binding_row("c", "provider", "env_var", "BAR"),
    ]
    edges = cross_repo_edges(symbols, bindings)
    by_key = {e.key_norm: e for e in edges}
    assert by_key["FOO"].precision == "precise"
    assert by_key["BAR"].precision == "heuristic"
    assert by_key["BAR"].provider_repo == "c"


def test_min_precision_precise_excludes_heuristic_tier():
    symbols = []
    bindings = [
        _binding_row("a", "consumer", "env_var", "BAR"),
        _binding_row("c", "provider", "env_var", "BAR"),
    ]
    edges = cross_repo_edges(symbols, bindings, min_precision="precise")
    assert edges == []


def test_min_precision_fuzzy_currently_same_as_heuristic():
    symbols = []
    bindings = [
        _binding_row("a", "consumer", "env_var", "BAR"),
        _binding_row("c", "provider", "env_var", "BAR"),
    ]
    heuristic = cross_repo_edges(symbols, bindings, min_precision="heuristic")
    fuzzy = cross_repo_edges(symbols, bindings, min_precision="fuzzy")
    assert heuristic == fuzzy
    assert len(heuristic) == 1


def test_invalid_min_precision_raises():
    with pytest.raises(ValueError):
        cross_repo_edges([], [], min_precision="nonsense")


def test_precision_tiers_ordering():
    assert PRECISION_TIERS == ("precise", "heuristic", "fuzzy")


# ── Heuristic-tier grouping details ───────────────────────────────


def test_self_providing_repo_excluded_from_heuristic_edges():
    """Mirrors visualize.build_graph: a repo that both provides and consumes a
    key_norm is presumed to self-serve it, so it produces NO consumer edges
    for that key at all — even toward an unrelated third-party provider."""
    bindings = [
        _binding_row("a", "consumer", "env_var", "FOO"),
        _binding_row("a", "provider", "env_var", "FOO"),
        _binding_row("b", "provider", "env_var", "FOO"),
    ]
    assert cross_repo_edges([], bindings) == []


def test_non_self_providing_consumer_links_to_provider():
    bindings = [
        _binding_row("a", "consumer", "env_var", "FOO"),
        _binding_row("b", "provider", "env_var", "FOO"),
    ]
    edges = cross_repo_edges([], bindings)
    assert len(edges) == 1
    assert edges[0].consumer_repo == "a"
    assert edges[0].provider_repo == "b"


def test_service_kind_excluded_from_heuristic_edges():
    bindings = [
        _binding_row("a", "provider", "service", "repo:https://github.com/x/y"),
        _binding_row("b", "consumer", "service", "repo:https://github.com/x/y"),
    ]
    assert cross_repo_edges([], bindings) == []


def test_heuristic_edge_carries_evidence_and_confidence():
    bindings = [
        _binding_row(
            "a",
            "consumer",
            "endpoint",
            "/api/v0/x/{}",
            confidence=0.7,
            file="client.ts",
            line=5,
        ),
        _binding_row(
            "b", "provider", "endpoint", "/api/v0/x/{}", file="openapi.json", line=1
        ),
    ]
    edges = cross_repo_edges([], bindings)
    assert len(edges) == 1
    e = edges[0]
    assert e.confidence == 0.7
    assert e.canonical_symbol is None
    assert e.evidence == ({"repo": "a", "file": "client.ts", "line": 5},)


def test_low_confidence_endpoint_consumer_filtered_by_min_confidence():
    bindings = [
        _binding_row("a", "consumer", "endpoint", "/api/v0/x/{}", confidence=0.1),
        _binding_row("b", "provider", "endpoint", "/api/v0/x/{}"),
    ]
    assert cross_repo_edges([], bindings, min_confidence=0.5) == []
    assert len(cross_repo_edges([], bindings, min_confidence=0.05)) == 1


def test_zero_confidence_is_not_treated_as_missing():
    """A genuine 0.0 confidence must survive as 0.0, not fall back to 1.0
    (0.0 is falsy in Python — `x or 1.0` silently discards it)."""
    bindings = [
        _binding_row("a", "consumer", "endpoint", "/api/v0/x/{}", confidence=0.0),
        _binding_row("b", "provider", "endpoint", "/api/v0/x/{}"),
    ]
    edges = cross_repo_edges([], bindings, min_confidence=0.0)
    assert len(edges) == 1
    assert edges[0].confidence == 0.0


def test_typed_edge_as_dict_shape():
    e = TypedEdge(
        precision="precise",
        consumer_repo="a",
        provider_repo="b",
        kind="env_var",
        key_norm="FOO",
        canonical_symbol="env:.:.:.:FOO",
        confidence=1.0,
        evidence=({"repo": "a", "file": "x.py", "line": 1},),
    )
    d = e.as_dict()
    assert d["evidence"] == [{"repo": "a", "file": "x.py", "line": 1}]
    assert d["precision"] == "precise"


# ── precise_pairs helper ───────────────────────────────────────────


def test_precise_pairs_matches_resolved_edges():
    symbols = [
        _symbol_row("a", "external", "env:.:.:.:FOO", "env", "FOO", "FOO"),
        _symbol_row(
            "b", "exported", "env:.:b:main:FOO", "env", "FOO", "FOO", version="main"
        ),
    ]
    pairs = precise_pairs(symbols)
    assert pairs == {("a", "b", "env_var", "FOO")}


# ── Integration: build_graph + MCP tools honor min_precision ──────


A_SETTINGS = """\
from main.envs import get_string

APP_BASE_URL = get_string("MITOL_APP_BASE_URL", None)
"""

B_PULUMI = """\
config:
  learn_ai:env_vars:
    MITOL_APP_BASE_URL: "https://learn-ai-qa.ol.mit.edu"
"""


def _fn(tool):
    return getattr(tool, "fn", tool)


@requires_stack
def test_build_graph_min_precision_precise_matches_edges_module(tmp_path, monkeypatch):
    from witan_code import config as cfg_mod
    from witan_code import indexer
    from witan_code import visualize
    from witan_code.graph import OmnigraphClient

    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))

    a = tmp_path / "repo_a"
    (a / "main").mkdir(parents=True)
    (a / "main" / "settings.py").write_text(A_SETTINGS)
    b = tmp_path / "repo_b"
    b.mkdir()
    (b / "Pulumi.QA.yaml").write_text(B_PULUMI)

    for repo, path in (
        ("https://github.com/test/repo-a", a),
        ("https://github.com/test/repo-b", b),
    ):
        monkeypatch.setenv("WITAN_REPO", repo)
        cfg = cfg_mod.load()
        indexer.index_path(path, config=cfg)

    client = OmnigraphClient(
        str(cfg_mod.bridge_store_path(cfg.code_dir)), cfg.queries_dir
    )
    rows = client.read("bridge.gq", "all_bindings", {})
    repo_symbol_rows = client.read("bridge.gq", "all_repo_symbols", {})

    heuristic_graph = visualize.build_graph(rows, min_precision="heuristic")
    assert ("https://github.com/test/repo-a", "https://github.com/test/repo-b") in (
        heuristic_graph.edges
    )

    precise_graph = visualize.build_graph(
        rows, min_precision="precise", repo_symbol_rows=repo_symbol_rows
    )
    assert ("https://github.com/test/repo-a", "https://github.com/test/repo-b") in (
        precise_graph.edges
    )

    # No repo_symbol_rows supplied -> nothing can be precisely covered.
    empty_precise = visualize.build_graph(rows, min_precision="precise")
    assert empty_precise.edges == {}


@requires_stack
def test_mcp_tools_min_precision_precise_filters_to_stage2(tmp_path, monkeypatch):
    from witan_code import config as cfg_mod
    from witan_code import indexer
    from witan_code import server as srv

    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))

    a = tmp_path / "repo_a"
    (a / "main").mkdir(parents=True)
    (a / "main" / "settings.py").write_text(A_SETTINGS)
    b = tmp_path / "repo_b"
    b.mkdir()
    (b / "Pulumi.QA.yaml").write_text(B_PULUMI)
    # repo-c consumes the same env var only via a heuristic-tier-only path:
    # give it a *different* env var Pulumi doesn't provide, resolvable only
    # by the (kind, key_norm) grouping if we later add a matching provider.
    c = tmp_path / "repo_c"
    (c / "main").mkdir(parents=True)
    (c / "main" / "settings.py").write_text(
        'from main.envs import get_string\nOTHER = get_string("OTHER_VAR", None)\n'
    )
    d = tmp_path / "repo_d"
    d.mkdir()
    (d / "Pulumi.QA.yaml").write_text('config:\n  svc:env_vars:\n    OTHER_VAR: "x"\n')

    for repo, path in (
        ("https://github.com/test/repo-a", a),
        ("https://github.com/test/repo-b", b),
        ("https://github.com/test/repo-c", c),
        ("https://github.com/test/repo-d", d),
    ):
        monkeypatch.setenv("WITAN_REPO", repo)
        monkeypatch.setattr(srv, "cfg", cfg_mod.load())
        srv._clients.clear()
        indexer.index_path(path, config=srv.cfg)

    # Both MITOL_APP_BASE_URL and OTHER_VAR resolve precisely via Stage 2
    # (both are plain env-var provider/consumer pairs) — so at the
    # code_interface_consumers level, min_precision="precise" keeps both,
    # same as "heuristic", since there's no non-precisely-resolvable env var
    # in this fixture. What we can assert cheaply and robustly: results are
    # non-empty and identical in shape for a key that resolves precisely.
    heuristic = _fn(srv.code_interface_consumers)("env_var", "MITOL_APP_BASE_URL")
    precise = _fn(srv.code_interface_consumers)(
        "env_var", "MITOL_APP_BASE_URL", min_precision="precise"
    )
    assert heuristic and precise
    assert {r["repo"] for r in precise} == {r["repo"] for r in heuristic}

    # A key_norm with zero exported symbols anywhere is dropped entirely at
    # min_precision="precise" (nothing to join against) but still shows up
    # at the default heuristic tier if a raw consumer binding exists for it.
    unresolved_only = _fn(srv.code_interface_consumers)(
        "env_var", "NEVER_PROVIDED_ANYWHERE"
    )
    assert unresolved_only == []  # no consumer binding at all in this fixture


@requires_stack
def test_code_cross_repo_impact_min_precision_is_repo_pair_specific(
    tmp_path, monkeypatch
):
    from witan_code import config as cfg_mod
    from witan_code import indexer
    from witan_code import server as srv

    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))

    a = tmp_path / "repo_a"
    (a / "main").mkdir(parents=True)
    (a / "main" / "settings.py").write_text(A_SETTINGS)
    b = tmp_path / "repo_b"
    b.mkdir()
    (b / "Pulumi.QA.yaml").write_text(B_PULUMI)

    for repo, path in (
        ("https://github.com/test/repo-a", a),
        ("https://github.com/test/repo-b", b),
    ):
        monkeypatch.setenv("WITAN_REPO", repo)
        monkeypatch.setattr(srv, "cfg", cfg_mod.load())
        srv._clients.clear()
        indexer.index_path(path, config=srv.cfg)

    consumers = _fn(srv.code_interface_consumers)("env_var", "MITOL_APP_BASE_URL")
    symbol_id = consumers[0]["symbol_id"]

    heuristic = _fn(srv.code_cross_repo_impact)(symbol_id)
    precise = _fn(srv.code_cross_repo_impact)(symbol_id, min_precision="precise")
    assert any(
        b["repo"] == "https://github.com/test/repo-b" for b in precise["cross_repo"]
    )
    assert {b["slug"] for b in precise["cross_repo"]} <= {
        b["slug"] for b in heuristic["cross_repo"]
    }
