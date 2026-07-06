"""Unit tests for the cross-repo dependency visualizer (no graph store needed)."""

import pytest

from witan_code import visualize

A = "https://github.com/mitodl/repo-a"
B = "https://github.com/mitodl/repo-b"
INFRA = "https://github.com/mitodl/ol-infrastructure"

ROWS = [
    # repo-a consumes an env var repo-b provides.
    {
        "kind": "env_var",
        "key_norm": "MITOL_APP_BASE_URL",
        "role": "consumer",
        "repo": A,
        "confidence": 1.0,
    },
    {
        "kind": "env_var",
        "key_norm": "MITOL_APP_BASE_URL",
        "role": "provider",
        "repo": B,
        "confidence": 1.0,
    },
    # repo-a calls an endpoint repo-b serves.
    {
        "kind": "endpoint",
        "key_norm": "/api/v1/courses/{}",
        "role": "consumer",
        "repo": A,
        "confidence": 0.8,
    },
    {
        "kind": "endpoint",
        "key_norm": "/api/v1/courses/{}",
        "role": "provider",
        "repo": B,
        "confidence": 1.0,
    },
    # ol-infrastructure deploys repo-a (service anchor).
    {
        "kind": "service",
        "key_norm": f"repo:{A}",
        "role": "provider",
        "repo": INFRA,
        "confidence": 1.0,
    },
    {
        "kind": "service",
        "key_norm": "name:repo-a",
        "role": "provider",
        "repo": INFRA,
        "confidence": 1.0,
    },
]


def test_build_graph_edges_and_weights():
    g = visualize.build_graph(ROWS)
    assert g.repos == {A, B, INFRA}
    # repo-a depends on repo-b via 2 contracts (env_var + endpoint).
    ab = g.edges[(A, B)]
    assert ab.weight == 2
    assert dict(ab.kinds) == {"env_var": 1, "endpoint": 1}
    # the edge carries the individual linkages for the HTML detail table.
    assert {(c["kind"], c["key"]) for c in ab.contracts} == {
        ("env_var", "MITOL_APP_BASE_URL"),
        ("endpoint", "/api/v1/courses/{}"),
    }
    # service edge records the deployed repo as its contract.
    assert g.edges[(INFRA, A)].contracts[0]["kind"] == "service"
    assert g.edges[(INFRA, A)].contracts[0]["key"] == "mitodl/repo-a"
    # ol-infra depends on repo-a via the service anchor; name: anchor is ignored.
    assert g.edges[(INFRA, A)].weight == 1
    assert set(g.edges[(INFRA, A)].kinds) == {"service"}


def test_kind_filter():
    g = visualize.build_graph(ROWS, kind="endpoint")
    assert list(g.edges) == [(A, B)]
    assert dict(g.edges[(A, B)].kinds) == {"endpoint": 1}


def test_repo_filter_keeps_touching_edges():
    g = visualize.build_graph(ROWS, repo="ol-infrastructure")
    assert list(g.edges) == [(INFRA, A)]


def test_short_repo():
    assert visualize.short_repo(A) == "mitodl/repo-a"
    assert visualize.short_repo("local-dir") == "local-dir"


def test_render_html_writes_self_contained_file(tmp_path):
    g = visualize.build_graph(ROWS)
    out = visualize.render_html(g, tmp_path / "deps.html")
    text = out.read_text()
    assert out.exists()
    assert "vis-network" in text
    assert "mitodl/repo-a" in text
    # nodes + edges are embedded as JSON, not fetched.
    assert '"from"' in text and '"to"' in text
    # per-edge linkage list + the click-to-show-table handler are present.
    assert '"contracts"' in text
    assert "MITOL_APP_BASE_URL" in text
    assert "function showEdge" in text


def test_render_rich_smoke():
    from rich.console import Console

    g = visualize.build_graph(ROWS)
    visualize.render_rich(g, console=Console(file=open("/dev/null", "w")))


def test_render_rich_wraps_long_repos_at_narrow_width():
    """Regression: long repo names must fold, not ellipsize (see PR #55).

    A narrow console forces the two repo columns to overflow. With the
    previous ``no_wrap=True`` config these were truncated with a Unicode
    ellipsis; with ``overflow="fold"`` they wrap over multiple lines
    instead.
    """
    import io

    from rich.console import Console

    long_src = "https://github.com/mitodl/some-really-really-long-source-repo"
    long_dst = "https://github.com/mitodl/some-really-really-long-provider-repo"
    g = visualize.DepGraph()
    g.edge(long_src, long_dst).add("endpoint", "/api/v1/thing")

    buf = io.StringIO()
    visualize.render_rich(g, console=Console(file=buf, width=60, force_terminal=False))
    out = buf.getvalue()

    assert "\u2026" not in out, (
        "render_rich ellipsized a cell instead of folding it — a wrapping "
        "column was likely marked no_wrap=True. Rendered output:\n" + out
    )


def test_self_provided_path_suppressed():
    """When a consumer repo also provides the same key_norm, no edge is emitted.

    Scenario: both repo-a and repo-b serve ``/api/v0/profiles/{}`` (e.g. both
    have a Django route for it).  Repo-a's TS code contains a path literal that
    matches the same key_norm.  Without the fix this would produce a phantom
    repo-a → repo-b edge.  With the fix, the edge must be absent because repo-a
    is itself a provider of that contract.
    """
    C = "https://github.com/mitodl/repo-c"
    rows = [
        # repo-a both provides AND consumes /api/v0/profiles/{} (same-origin route).
        {
            "kind": "endpoint",
            "key_norm": "/api/v0/profiles/{}",
            "role": "provider",
            "repo": A,
            "confidence": 1.0,
        },
        {
            "kind": "endpoint",
            "key_norm": "/api/v0/profiles/{}",
            "role": "consumer",
            "repo": A,
            "confidence": 0.8,
        },
        # repo-b also provides the route (shared path key collision).
        {
            "kind": "endpoint",
            "key_norm": "/api/v0/profiles/{}",
            "role": "provider",
            "repo": B,
            "confidence": 1.0,
        },
        # repo-c is a genuine external consumer (does not provide the route).
        {
            "kind": "endpoint",
            "key_norm": "/api/v0/profiles/{}",
            "role": "consumer",
            "repo": C,
            "confidence": 0.8,
        },
    ]
    g = visualize.build_graph(rows)
    # repo-a self-provides → must NOT generate a → b edge.
    assert (A, B) not in g.edges, "phantom edge: repo-a→repo-b should be suppressed"
    # repo-c is a genuine external consumer → must generate edges to both a and b.
    assert (C, A) in g.edges, "real consumer edge C→A should exist"
    assert (C, B) in g.edges, "real consumer edge C→B should exist"


# ── cross_repo_edges and min_confidence tests ─────────────────────


def test_cross_repo_edges_filters_low_confidence_endpoints():
    """cross_repo_edges drops endpoint consumers below min_confidence."""
    rows = [
        {
            "kind": "endpoint",
            "key_norm": "/api/v0/x/",
            "role": "consumer",
            "repo": A,
            "confidence": 0.2,
        },
        {
            "kind": "endpoint",
            "key_norm": "/api/v0/x/",
            "role": "provider",
            "repo": B,
            "confidence": 1.0,
        },
        {
            "kind": "env_var",
            "key_norm": "FOO",
            "role": "consumer",
            "repo": A,
            "confidence": 1.0,
        },
    ]
    filtered = visualize.cross_repo_edges(rows, min_confidence=0.5)
    kinds_roles = [(r["kind"], r["role"]) for r in filtered]
    # low-confidence endpoint consumer is dropped
    assert ("endpoint", "consumer") not in kinds_roles
    # provider passes through
    assert ("endpoint", "provider") in kinds_roles
    # env_var consumer passes through (not an endpoint)
    assert ("env_var", "consumer") in kinds_roles


def test_cross_repo_edges_passes_high_confidence_endpoints():
    """cross_repo_edges keeps endpoint consumers at or above min_confidence."""
    rows = [
        {
            "kind": "endpoint",
            "key_norm": "/api/v0/x/",
            "role": "consumer",
            "repo": A,
            "confidence": 0.7,
        },
        {
            "kind": "endpoint",
            "key_norm": "/api/v0/x/",
            "role": "provider",
            "repo": B,
            "confidence": 1.0,
        },
    ]
    filtered = visualize.cross_repo_edges(rows, min_confidence=0.5)
    assert len(filtered) == 2


def test_cross_repo_edges_legacy_rows_without_confidence():
    """Rows without a confidence key default to 1.0 and pass through."""
    rows = [
        {"kind": "endpoint", "key_norm": "/api/v0/x/", "role": "consumer", "repo": A},
        {"kind": "endpoint", "key_norm": "/api/v0/x/", "role": "provider", "repo": B},
    ]
    filtered = visualize.cross_repo_edges(rows, min_confidence=0.5)
    assert len(filtered) == 2


def test_build_graph_min_confidence_filters_endpoint_consumers():
    """build_graph with min_confidence=0.8 suppresses low-confidence endpoint consumers."""
    rows = [
        # High-confidence endpoint consumer — should produce an edge.
        {
            "kind": "endpoint",
            "key_norm": "/api/v1/courses/{}",
            "role": "consumer",
            "repo": A,
            "confidence": 0.9,
        },
        {
            "kind": "endpoint",
            "key_norm": "/api/v1/courses/{}",
            "role": "provider",
            "repo": B,
            "confidence": 1.0,
        },
        # Low-confidence endpoint consumer — should be filtered out at min_confidence=0.8.
        {
            "kind": "endpoint",
            "key_norm": "/api/v0/low/",
            "role": "consumer",
            "repo": A,
            "confidence": 0.3,
        },
        {
            "kind": "endpoint",
            "key_norm": "/api/v0/low/",
            "role": "provider",
            "repo": B,
            "confidence": 1.0,
        },
    ]
    g = visualize.build_graph(rows, min_confidence=0.8)
    # High-confidence edge exists.
    assert (A, B) in g.edges
    assert g.edges[(A, B)].weight == 1
    # The low-confidence contract key should not appear in any contract.
    all_keys = {c["key"] for e in g.edges.values() for c in e.contracts}
    assert "/api/v0/low/" not in all_keys, "low-confidence consumer should be filtered"


def test_build_graph_default_min_confidence_suppresses_very_low():
    """build_graph default (min_confidence=0.5) suppresses sub-0.5 consumers."""
    rows = [
        {
            "kind": "endpoint",
            "key_norm": "/api/v0/test/",
            "role": "consumer",
            "repo": A,
            "confidence": 0.1,
        },
        {
            "kind": "endpoint",
            "key_norm": "/api/v0/test/",
            "role": "provider",
            "repo": B,
            "confidence": 1.0,
        },
    ]
    g = visualize.build_graph(rows)
    assert (A, B) not in g.edges, "sub-0.5 endpoint consumer should produce no edge"


def test_build_graph_confidence_on_emitted_edge_contracts():
    """Contracts emitted in edges carry the confidence value."""
    rows = [
        {
            "kind": "endpoint",
            "key_norm": "/api/v1/courses/{}",
            "role": "consumer",
            "repo": A,
            "confidence": 0.75,
        },
        {
            "kind": "endpoint",
            "key_norm": "/api/v1/courses/{}",
            "role": "provider",
            "repo": B,
            "confidence": 1.0,
        },
    ]
    g = visualize.build_graph(rows)
    assert (A, B) in g.edges
    ep_contracts = [c for c in g.edges[(A, B)].contracts if c["kind"] == "endpoint"]
    assert ep_contracts, "endpoint contract should be present in edge"
    assert ep_contracts[0]["confidence"] == 0.75


def test_build_graph_rejects_invalid_min_precision():
    with pytest.raises(ValueError):
        visualize.build_graph(ROWS, min_precision="nonsense")
