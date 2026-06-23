"""Unit tests for the cross-repo dependency visualizer (no graph store needed)."""

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
    },
    {
        "kind": "env_var",
        "key_norm": "MITOL_APP_BASE_URL",
        "role": "provider",
        "repo": B,
    },
    # repo-a calls an endpoint repo-b serves.
    {
        "kind": "endpoint",
        "key_norm": "/api/v1/courses/{}",
        "role": "consumer",
        "repo": A,
    },
    {
        "kind": "endpoint",
        "key_norm": "/api/v1/courses/{}",
        "role": "provider",
        "repo": B,
    },
    # ol-infrastructure deploys repo-a (service anchor).
    {"kind": "service", "key_norm": f"repo:{A}", "role": "provider", "repo": INFRA},
    {"kind": "service", "key_norm": "name:repo-a", "role": "provider", "repo": INFRA},
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
    assert g.edges[(INFRA, A)].contracts == [
        {"kind": "service", "key": "mitodl/repo-a"}
    ]
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


def test_self_provided_path_suppressed():
    """Fix 3: when a consumer repo also provides the same key_norm, no edge is emitted.

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
        },
        {
            "kind": "endpoint",
            "key_norm": "/api/v0/profiles/{}",
            "role": "consumer",
            "repo": A,
        },
        # repo-b also provides the route (shared path key collision).
        {
            "kind": "endpoint",
            "key_norm": "/api/v0/profiles/{}",
            "role": "provider",
            "repo": B,
        },
        # repo-c is a genuine external consumer (does not provide the route).
        {
            "kind": "endpoint",
            "key_norm": "/api/v0/profiles/{}",
            "role": "consumer",
            "repo": C,
        },
    ]
    g = visualize.build_graph(rows)
    # repo-a self-provides → must NOT generate a → b edge.
    assert (A, B) not in g.edges, "phantom edge: repo-a→repo-b should be suppressed"
    # repo-c is a genuine consumer → must generate a c → b edge (and c → a edge).
    assert (C, A) in g.edges or (C, B) in g.edges, "real consumer edge should exist"
