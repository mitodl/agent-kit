"""End-to-end tests for the tree-sitter indexer and omnigraph store queries."""

from .conftest import requires_stack


@requires_stack
def test_index_extracts_symbols_and_edges(sample_repo):
    from omnigraph_codegraph import config as cfg_mod
    from omnigraph_codegraph import indexer
    from omnigraph_codegraph import repo as repo_mod
    from omnigraph_codegraph import store as store_mod
    from omnigraph_codegraph.graph import OmnigraphClient

    cfg = cfg_mod.load()
    stats = indexer.index_path(sample_repo, config=cfg)
    assert stats.indexed >= 1
    assert stats.symbols >= 3
    assert stats.errors == 0

    slug = repo_mod.detect()
    store = store_mod.store_for_repo(slug, cfg)
    client = OmnigraphClient(str(store), cfg.queries_dir)

    runs = client.read("read.gq", "find_by_name", {"name": "run"})
    assert any(r["qualified_name"] == "Service.run" for r in runs)

    helpers = client.read("read.gq", "find_by_name", {"name": "helper"})
    assert helpers
    # heuristic Calls edge traversal: Service.run calls helper
    callers = client.read("read.gq", "callers", {"id": helpers[0]["slug"]})
    assert "Service.run" in {c["qualified_name"] for c in callers}

    hits = client.read("read.gq", "search_symbols", {"query": "helper"})
    assert any(h["name"] == "helper" for h in hits)


@requires_stack
def test_incremental_reindex_skips_unchanged(sample_repo):
    from omnigraph_codegraph import config as cfg_mod
    from omnigraph_codegraph import indexer

    cfg = cfg_mod.load()
    first = indexer.index_path(sample_repo, config=cfg)
    assert first.indexed >= 1

    second = indexer.index_path(sample_repo, config=cfg)
    assert second.indexed == 0
    assert second.skipped >= 1
