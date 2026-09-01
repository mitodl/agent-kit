"""End-to-end tests for the tree-sitter indexer and omnigraph store queries."""

from pathlib import Path

from .conftest import requires_stack


@requires_stack
def test_index_extracts_symbols_and_edges(sample_repo):
    from witan_code import config as cfg_mod
    from witan_code import indexer
    from witan_code import repo as repo_mod
    from witan_code import store as store_mod
    from witan_code.graph import OmnigraphClient

    cfg = cfg_mod.load()
    stats = indexer.index_path(sample_repo, config=cfg)
    assert stats.indexed >= 1
    assert stats.symbols >= 3
    assert stats.errors == 0

    slug = repo_mod.detect()
    store = store_mod.store_for_repo(slug, cfg)
    # The destination the summary reports comes off the run itself, so this is
    # what stops that line drifting from where the rows actually landed.
    assert stats.store == str(store)
    client = OmnigraphClient(str(store), cfg.queries_dir)

    runs = client.read("code_read.gq", "find_by_name", {"name": "run"})
    assert any(r["qualified_name"] == "Service.run" for r in runs)

    helpers = client.read("code_read.gq", "find_by_name", {"name": "helper"})
    assert helpers
    # heuristic Calls edge traversal: Service.run calls helper
    callers = client.read("code_read.gq", "callers", {"id": helpers[0]["slug"]})
    assert "Service.run" in {c["qualified_name"] for c in callers}

    hits = client.read("code_read.gq", "search_symbols", {"query": "helper"})
    assert any(h["name"] == "helper" for h in hits)


@requires_stack
def test_incremental_reindex_skips_unchanged(sample_repo):
    from witan_code import config as cfg_mod
    from witan_code import indexer

    cfg = cfg_mod.load()
    first = indexer.index_path(sample_repo, config=cfg)
    assert first.indexed >= 1

    second = indexer.index_path(sample_repo, config=cfg)
    assert second.indexed == 0
    assert second.skipped >= 1


# ── A failing bridge write must be reported, not swallowed ──────────────────
#
# It was swallowed, and that cost 15 hours of silently-frozen cross-repo
# bindings on production: every CI cycle logged a warning nothing read,
# reported `bindings=0 errors=0`, and exited 0. Sentry never saw it because its
# LoggingIntegration fires at ERROR and the site logged at WARNING — which by
# that integration's own contract declares a failure "expected and already
# handled".


def _failing_bridge(monkeypatch):
    """Make the bridge write raise the way the production barrier did."""
    from witan_code import bridge as bridge_module

    def _raise(*args, **kwargs):
        raise RuntimeError(
            "omnigraph branch failed after 6 attempts — a recovery barrier on "
            "the branch kept blocking the read"
        )

    monkeypatch.setattr(bridge_module, "write_bindings", _raise)


@requires_stack
def test_a_failing_bridge_write_is_counted_and_flagged(sample_repo, monkeypatch):
    """`bindings=0` alone cannot distinguish "nothing to write" from "the write
    threw", so the failure has to show up as both an error count and a flag."""
    from witan_code import config as cfg_mod
    from witan_code import indexer

    _failing_bridge(monkeypatch)

    stats = indexer.index_path(sample_repo, config=cfg_mod.load())

    assert stats.bridge_failed is True
    assert stats.errors >= 1
    # Still not fatal: the per-repo half succeeded and is worth keeping.
    assert stats.indexed >= 1
    assert stats.symbols >= 1


@requires_stack
def test_a_failing_bridge_write_logs_at_error_so_sentry_sees_it(
    sample_repo, monkeypatch
):
    """THE level is the mechanism.

    `configure_sentry` installs `LoggingIntegration(event_level=ERROR)` so a
    site like this needs no `capture_exception` call. That makes the level the
    difference between a Sentry issue and a breadcrumb, which is why this
    asserts it explicitly rather than trusting the call site to stay right.
    """
    import structlog

    from witan_code import config as cfg_mod
    from witan_code import indexer

    _failing_bridge(monkeypatch)

    with structlog.testing.capture_logs() as entries:
        indexer.index_path(sample_repo, config=cfg_mod.load())

    bridge = [e for e in entries if e.get("event") == "witan.code.index.bridge_failed"]
    assert bridge, "the bridge failure was not logged at all"
    assert bridge[0]["log_level"] == "error", (
        f"logged at {bridge[0]['log_level']!r}; Sentry's event_level is ERROR, "
        "so anything below it is a breadcrumb and raises no issue"
    )


def test_the_summary_line_says_when_the_bridge_failed():
    """The one line anybody reads has to carry it."""
    from witan_code import cli, indexer

    printed: list[str] = []
    ok = indexer.IndexStats(scanned=1, indexed=1, bindings=0)
    failed = indexer.IndexStats(scanned=1, indexed=1, bindings=0, bridge_failed=True)

    import builtins

    original = builtins.print
    builtins.print = lambda *a, **k: printed.append(" ".join(str(x) for x in a))
    try:
        cli._print_summary("index", Path("."), ok)
        cli._print_summary("index", Path("."), failed)
    finally:
        builtins.print = original

    assert "bridge=FAILED" not in printed[0], "a clean run must not cry wolf"
    assert "bridge=FAILED" in printed[1]
