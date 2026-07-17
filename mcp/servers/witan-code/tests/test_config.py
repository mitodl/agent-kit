"""Canonical cluster graph-id derivation (config.graph_id).

This id is a cross-repo contract: witan-code addresses `--graph <id>` and
ol-infrastructure's cluster.yaml provisioning declares the same `<id>`. If this
algorithm drifts from the provisioning side, clients address graphs the cluster
never created — so pin the observable mapping here.
"""

from witan_code import config


def test_graph_id_canonical_example():
    # The example from the deployment decision; must be byte-for-byte stable.
    assert (
        config.graph_id("https://github.com/mitodl/ol-django")
        == "code-github-com-mitodl-ol-django"
    )


def test_graph_id_matches_constraint():
    for repo in (
        "https://github.com/mitodl/ol-django",
        "http://example.com/x",
        "git@github.com:mitodl/agent-kit.git",
        "github.com/mitodl/ol-infrastructure",
        "https://github.com/MITODL/Mixed_Case.Repo",
    ):
        assert config.GRAPH_ID_RE.match(config.graph_id(repo)), repo


def test_graph_id_no_underscores_and_lowercase():
    gid = config.graph_id("https://github.com/MITODL/Mixed_Case.Repo")
    assert "_" not in gid
    assert gid == gid.lower()


def test_graph_id_strips_scheme():
    with_scheme = config.graph_id("https://github.com/mitodl/x")
    without = config.graph_id("github.com/mitodl/x")
    assert with_scheme == without == "code-github-com-mitodl-x"


def test_graph_id_is_prefixed():
    assert config.graph_id("github.com/a/b").startswith(config.CODE_GRAPH_PREFIX)


def test_graph_id_long_repo_truncates_with_hash():
    long_repo = "https://github.com/mitodl/" + "a" * 200
    gid = config.graph_id(long_repo)
    assert len(gid) <= 64
    assert config.GRAPH_ID_RE.match(gid)
    # Distinct long repos disambiguate via the URI hash, never colliding.
    other = config.graph_id("https://github.com/mitodl/" + "a" * 199 + "b")
    assert gid != other


def test_graph_id_is_deterministic():
    repo = "https://github.com/mitodl/" + "z" * 100
    assert config.graph_id(repo) == config.graph_id(repo)


def test_bridge_graph_id_constant():
    assert config.BRIDGE_GRAPH_ID == "code-bridge"
    assert config.GRAPH_ID_RE.match(config.BRIDGE_GRAPH_ID)
