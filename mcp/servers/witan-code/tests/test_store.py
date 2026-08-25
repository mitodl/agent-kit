"""Store stat helpers backing `code_indexed_repos` and the prompt-hook block.

Both run per store — the hook on every single prompt — so these avoid bulk
reads and per-entry Path construction. That makes their edge cases (an empty
store, an unreadable one, a dangling symlink) worth pinning explicitly.
"""

import os
import threading
import time
from pathlib import Path

import pytest

from witan_core import omnigraph_http as http_module

from witan_code import store as store_module

from .conftest import requires_stack


class _StubClient:
    def __init__(self, rows):
        self._rows = rows

    def read(self, *_args, **_kwargs):
        return self._rows


class _StubConfig:
    queries_dir = Path(".")


# ── file_count ────────────────────────────────────────────────────────────────


@requires_stack
def test_file_count_agrees_with_the_indexed_file_set(sample_repo):
    """Counted in the engine now, so it must still match the actual rows."""
    from witan_code import config as cfg_mod
    from witan_code import indexer
    from witan_code.graph import OmnigraphClient

    cfg = cfg_mod.load()
    indexer.index_path(sample_repo, force=False, config=cfg)
    store = store_module.store_for_repo("https://github.com/test/cg", cfg)

    rows = OmnigraphClient(str(store), cfg.queries_dir).read(
        "code_read.gq", "all_file_hashes", {}
    )
    assert len(rows) >= 1
    assert store_module.file_count(store, cfg) == len(rows)


def test_file_count_reads_the_column_positionally(tmp_path, monkeypatch):
    """count_files names its column after the match variable on a populated
    store but "?" on an empty one, so the value cannot be looked up by key."""
    ref = store_module.StoreRef(str(tmp_path / "x.omni"))
    monkeypatch.setattr(
        store_module, "OmnigraphClient", lambda *a, **kw: _StubClient([{"f": 990}])
    )
    assert store_module.file_count(ref, _StubConfig()) == 990

    monkeypatch.setattr(
        store_module, "OmnigraphClient", lambda *a, **kw: _StubClient([{"?": 0}])
    )
    assert store_module.file_count(ref, _StubConfig()) == 0


def test_file_count_is_none_when_the_store_cannot_be_read(tmp_path, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("no such store")

    monkeypatch.setattr(store_module, "OmnigraphClient", _boom)
    ref = store_module.StoreRef(str(tmp_path / "x.omni"))
    assert store_module.file_count(ref, _StubConfig()) is None


# ── store_health ──────────────────────────────────────────────────────────────

# The real thing, from omnigraph 0.10.0 against a store 0.8.x wrote.
_STALE_SCHEMA_ERROR = (
    "__manifest is stamped at internal schema v4, but this omnigraph reads "
    "only v6. This graph was created by omnigraph 0.8.x."
)


def _raising_client(monkeypatch, message: str) -> None:
    def _boom(*_args, **_kwargs):
        raise RuntimeError(message)

    monkeypatch.setattr(store_module, "OmnigraphClient", _boom)


def test_store_health_names_a_stale_on_disk_format_as_such(tmp_path, monkeypatch):
    """The one unreadable-store failure with a known remedy.

    Every code_* tool against such a store errors identically and forever, and
    it is fixed by reindexing rather than by waiting or retrying — so it has to
    be distinguishable from a store that is merely unreachable right now.
    """
    _raising_client(monkeypatch, _STALE_SCHEMA_ERROR)
    ref = store_module.StoreRef(str(tmp_path / "x.omni"))

    health = store_module.store_health(ref, _StubConfig())

    assert health.ok is False
    assert health.stale_schema is True
    assert health.files is None
    assert "internal schema v4" in health.error


def test_store_health_does_not_call_every_other_failure_stale(tmp_path, monkeypatch):
    _raising_client(monkeypatch, "connection refused")
    ref = store_module.StoreRef(str(tmp_path / "x.omni"))

    health = store_module.store_health(ref, _StubConfig())

    assert health.ok is False
    assert health.stale_schema is False


def test_store_health_reports_an_empty_store_as_readable(tmp_path, monkeypatch):
    """0 files and "cannot be opened" must not render the same.

    Collapsing them is what let a dead code graph read as an under-indexed
    repo for six weeks.
    """
    monkeypatch.setattr(
        store_module, "OmnigraphClient", lambda *a, **kw: _StubClient([{"?": 0}])
    )
    ref = store_module.StoreRef(str(tmp_path / "x.omni"))

    health = store_module.store_health(ref, _StubConfig())

    assert health.ok is True
    assert health.files == 0


def test_health_report_covers_the_bridge_not_only_the_repo_stores(
    tmp_path, monkeypatch
):
    """The bridge is the store nothing else watches.

    It belongs to no repo, so per_repo_stores excludes it by design — and every
    code_interface_* / code_cross_repo_impact tool reads it and nothing else
    does. A readiness check that walks only the repo stores reports a healthy
    system while cross-repo resolution is entirely broken.
    """
    code_dir = tmp_path / "code"
    (code_dir / "https_github.com_test_cg.omni").mkdir(parents=True)
    (code_dir / "_bridge.omni").mkdir()
    monkeypatch.setenv("WITAN_CODE_DIR", str(code_dir))
    monkeypatch.setattr(
        store_module, "OmnigraphClient", lambda *a, **kw: _StubClient([{"f": 1}])
    )

    from witan_code import config as cfg_mod

    probed = {str(h.ref) for h in store_module.health_report(cfg_mod.load())}

    assert str(code_dir / "_bridge.omni") in probed
    assert str(code_dir / "https_github.com_test_cg.omni") in probed


# ── discard_store ─────────────────────────────────────────────────────────────


def test_discard_store_takes_the_schema_stamp_with_it(tmp_path):
    """A stamp outliving its store would suppress the apply on the fresh one.

    schema_apply_if_changed skips when the stamped mtime matches the schema
    file's, so a leftover stamp leaves the rebuilt store without its FTS
    indexes — a rebuild that silently produces a half-working graph.
    """
    from witan_core.omnigraph import schema_stamp_path

    store = tmp_path / "x.omni"
    store.mkdir()
    (store / "data.lance").write_text("payload")
    schema_stamp_path(store).write_text("123.0")
    store_module.repo_sidecar(store).write_text("https://github.com/test/cg")

    freed = store_module.discard_store(store_module.StoreRef(str(store)))

    assert not store.exists()
    assert not schema_stamp_path(store).exists()
    assert not store_module.repo_sidecar(store).exists()
    assert freed >= len("payload")


def test_discard_store_refuses_a_cluster_graph():
    """The client cannot create a cluster graph, so it may not delete one."""
    ref = store_module.StoreRef("https://omnigraph.test", graph_id="code-cg")

    with pytest.raises(ValueError, match="cluster graph"):
        store_module.discard_store(ref)


# ── dir_stats ─────────────────────────────────────────────────────────────────


def test_dir_stats_sums_sizes_and_takes_the_latest_mtime(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "a.bin").write_bytes(b"x" * 10)
    (tmp_path / "nested" / "b.bin").write_bytes(b"y" * 5)
    os.utime(tmp_path / "nested" / "b.bin", (1_800_000_000, 1_800_000_000))

    total, mtime = store_module.dir_stats(tmp_path)

    assert total == 15
    assert mtime >= 1_800_000_000


def test_dir_stats_skips_a_dangling_symlink(tmp_path):
    """The rglob + is_file() form this replaced skipped these silently; os.walk
    lists them, so the stat has to be guarded or a dead link would raise."""
    (tmp_path / "real.bin").write_bytes(b"z" * 7)
    (tmp_path / "dangling").symlink_to(tmp_path / "gone.bin")

    total, _mtime = store_module.dir_stats(tmp_path)

    assert total == 7


def test_dir_stats_does_not_descend_into_symlinked_directories(tmp_path):
    """os.walk(followlinks=False) matches rglob's behavior — a store that links
    to a large tree elsewhere must not have that tree's bytes attributed to it.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"q" * 1000)

    store = tmp_path / "store"
    store.mkdir()
    (store / "own.bin").write_bytes(b"w" * 3)
    (store / "link").symlink_to(outside, target_is_directory=True)

    total, _mtime = store_module.dir_stats(store)

    assert total == 3


# ── Cluster addressing ────────────────────────────────────────────────────────
#
# `code_server` is what turns every code graph from a directory on this machine
# into a graph on the shared omnigraph-server. These pin the three things that
# have to agree with ol-infrastructure's provisioning for that to work at all:
# the graph id, the flags it becomes, and what happens when the graph isn't
# there.


class _FakeClient:
    """Stands in for the per-graph client the cluster probe goes through.

    Records every ``list_branches`` call so a test can pin WHICH question was
    asked — the whole bug this path was rewritten for was asking the
    server-scoped one.
    """

    calls: list[tuple[str, str | None, bool]] = []

    def __init__(
        self,
        uri,
        queries_dir,
        *,
        token=None,
        branch=None,
        graph_id=None,
        connect_retry=True,
    ):  # noqa: PLR0913
        self.uri, self.graph_id, self.connect_retry = uri, graph_id, connect_retry
        self.served: frozenset[str] = frozenset()
        self.error: Exception | None = None

    def list_branches(self):
        type(self).calls.append((self.uri, self.graph_id, self.connect_retry))
        if self.error is not None:
            raise self.error
        if self.graph_id not in self.served:
            raise RuntimeError(
                f"omnigraph branch failed (exit 1):\ngraph '{self.graph_id}' not found"
            )
        return ["main"]


def _cluster(monkeypatch, *graphs: str, url: str = "https://omnigraph.test"):
    """Point config at a cluster serving exactly ``graphs``.

    Stubs both questions a caller can ask: the graph-scoped branch listing the
    existence probe uses, and the server-scoped registry ``per_repo_stores``
    enumerates from.
    """
    monkeypatch.setenv("WITAN_CODE_SERVER", url)
    monkeypatch.setattr(
        store_module, "cluster_graphs", lambda *a, **kw: frozenset(graphs)
    )
    monkeypatch.setattr(
        store_module, "safe_cluster_graphs", lambda *a, **kw: frozenset(graphs)
    )
    _FakeClient.calls = []

    def _build(*args, **kwargs):
        client = _FakeClient(*args, **kwargs)
        client.served = frozenset(graphs)
        return client

    monkeypatch.setattr(store_module, "OmnigraphClient", _build)


# The exact message omnigraph-server 0.8.1 returns for an unusable credential.
# Verbatim, because the whole point of the two exception types is telling THIS
# apart from a graph that genuinely isn't provisioned, and a paraphrase would
# stop testing that.
_MISSING_TOKEN_ERROR = "missing bearer token"


def _failing_graphs_list(monkeypatch, error: str = _MISSING_TOKEN_ERROR):
    """Make the server-scoped listing fail.

    Over HTTP now, not a subprocess: `omnigraph graphs list` cannot answer this
    against a multi-graph server at all (it fetches the listing and refuses to
    print it), so `cluster_graphs` asks `GET /graphs` directly.
    """
    monkeypatch.setattr(
        store_module,
        "shared_transport",
        lambda url: _FakeTransport(
            http_module.Outcome(kind=http_module.FATAL, error=error, status=401)
        ),
    )
    store_module.reset_graph_cache()


def _unreachable_client(monkeypatch, exc: Exception):
    """Make the graph-scoped probe fail with ``exc`` (no graphs stubbed)."""
    _FakeClient.calls = []

    def _build(*args, **kwargs):
        client = _FakeClient(*args, **kwargs)
        client.error = exc
        return client

    monkeypatch.setattr(store_module, "OmnigraphClient", _build)


def test_the_existence_probe_is_graph_scoped_not_server_scoped(monkeypatch):
    """REGRESSION: `graphs list` is a management-surface action that omnigraph
    closes by default, so depending on it here failed every environment's
    indexer before it parsed a file. The question "is MY graph served" is
    graph-scoped and answerable with `read`, which every actor holds."""
    from witan_code import config as cfg_module

    repo = "https://github.com/mitodl/ol-django"
    _cluster(monkeypatch, cfg_module.graph_id(repo))
    # Enumerating must not even be attempted on the write path — the deployed
    # server cannot answer it.
    monkeypatch.setattr(
        store_module,
        "cluster_graphs",
        lambda *a, **kw: pytest.fail("ensure_store must not call `graphs list`"),
    )
    cfg = cfg_module.load()

    store_module.ensure_store(repo, cfg)

    assert _FakeClient.calls == [
        ("https://omnigraph.test", cfg_module.graph_id(repo), True)
    ]


def test_the_write_path_probe_waits_out_a_restarting_server(monkeypatch):
    """`ensure_store` guards a whole index run, so an unreachable server is
    worth riding out — unlike a listing, which degrades to "no" instead."""
    from witan_code import config as cfg_module

    repo = "https://github.com/mitodl/ol-django"
    _cluster(monkeypatch, cfg_module.graph_id(repo))
    cfg = cfg_module.load()

    store_module.ensure_store(repo, cfg)
    assert [c[2] for c in _FakeClient.calls] == [True]

    _FakeClient.calls = []
    store_module.store_for_repo(repo, cfg).exists(cfg)
    assert [c[2] for c in _FakeClient.calls] == [False]


def test_unreachable_server_is_not_reported_as_an_unprovisioned_graph(monkeypatch):
    """An auth failure and "that graph does not exist" are the same failed
    probe. Collapsing them sent the first live run to check provisioning for
    what was really a missing bearer token."""
    from witan_code import config as cfg_module

    monkeypatch.setenv("WITAN_CODE_SERVER", "https://omnigraph.test")
    _unreachable_client(
        monkeypatch,
        RuntimeError("omnigraph branch failed (exit 1):\nmissing bearer token"),
    )
    cfg = cfg_module.load()

    with pytest.raises(store_module.ClusterUnreachable) as exc:
        store_module.ensure_store("https://github.com/mitodl/agent-kit", cfg)

    message = str(exc.value)
    assert "missing bearer token" in message  # the server's own words survive
    assert "data_tier.py" not in message  # and WITHOUT blaming provisioning


def test_listings_still_degrade_when_the_server_cannot_be_asked(monkeypatch):
    """A read path has nothing better to do with an unreachable server than
    report nothing — it must not take down every `code_*` tool."""
    from witan_code import config as cfg_module

    monkeypatch.setenv("WITAN_CODE_SERVER", "https://omnigraph.test")
    _failing_graphs_list(monkeypatch)  # the enumerating half, still a subprocess
    _unreachable_client(monkeypatch, RuntimeError("tcp connect error"))
    cfg = cfg_module.load()

    assert store_module.per_repo_stores(cfg) == []
    assert not store_module.store_for_repo("https://github.com/x/y", cfg).exists(cfg)


def test_a_relayed_refusal_survives_the_mcp_round_trip(monkeypatch):
    """Through the MCP tier the deployment runs `probe_cluster_graph` on its
    own direct connection and relays ITS refusal as the tool error — so the
    message this function raises has to be one it also recognizes. Matching
    only the omnigraph CLI's wording filed every missing graph reached through
    the deployment as merely unreachable."""
    from witan_code import config as cfg_module

    _cluster(monkeypatch, "code-github-com-mitodl-ol-django")
    cfg = cfg_module.load()

    with pytest.raises(store_module.ClusterGraphMissing) as direct:
        store_module.ensure_store("https://github.com/test/never-provisioned", cfg)

    # Exactly what the deployment would hand back, verbatim.
    _unreachable_client(monkeypatch, RuntimeError(str(direct.value)))

    with pytest.raises(store_module.ClusterGraphMissing):
        store_module.ensure_store("https://github.com/test/never-provisioned", cfg)


def test_the_local_fallback_hint_names_the_setting_actually_routing(monkeypatch):
    """`code_server` is not what routes an MCP-tier client — `code_transport`
    is — so naming it there sends the reader to unset something inert."""
    from witan_code import config as cfg_module

    monkeypatch.setenv("WITAN_CODE_SERVER", "https://omnigraph.test")
    monkeypatch.setenv("WITAN_CODE_TRANSPORT", cfg_module.CODE_TRANSPORT_MCP)
    monkeypatch.setenv("WITAN_REMOTE_URL", "https://witan.test")
    monkeypatch.setenv("WITAN_OIDC_ISSUER", "https://sso.test/realms/ol")

    # The real RemoteStoreClient over a session that refuses — so this goes
    # through the MCP transport, not the subprocess one.
    class _Session:
        url = "https://witan.test"

        def call(self, *_args, **_kwargs):
            raise RuntimeError("graph 'whatever' not found")

    monkeypatch.setattr(store_module, "mcp_session", lambda *a, **kw: _Session())
    cfg = cfg_module.load()

    with pytest.raises(store_module.ClusterGraphMissing) as exc:
        store_module.ensure_store("https://github.com/test/nope", cfg)

    assert "code_transport" in str(exc.value)
    assert "code_server" not in str(exc.value)


def test_a_failed_listing_is_not_cached(monkeypatch):
    """A transient outage must not pin an error for the whole TTL — the next
    call has to be able to succeed."""
    from witan_code import config as cfg_module

    monkeypatch.setenv("WITAN_CODE_SERVER", "https://omnigraph.test")
    cfg = cfg_module.load()
    _failing_graphs_list(monkeypatch)
    assert store_module.per_repo_stores(cfg) == []

    monkeypatch.setattr(
        store_module,
        "shared_transport",
        lambda url: _FakeTransport(
            http_module.Outcome(
                kind=http_module.OK, body='{"graphs": [{"graph_id": "code-x-y"}]}'
            )
        ),
    )
    assert [r.graph_id for r in store_module.per_repo_stores(cfg)] == ["code-x-y"]


def test_store_for_repo_addresses_the_cluster_graph(monkeypatch):
    from witan_code import config as cfg_module

    monkeypatch.setenv("WITAN_CODE_SERVER", "https://omnigraph.test/")
    monkeypatch.setenv("WITAN_CODE_TOKEN", "tok")
    cfg = cfg_module.load()

    ref = store_module.store_for_repo("https://github.com/mitodl/ol-django", cfg)

    assert ref.is_remote
    # The trailing slash is stripped: the client splits scheme/host itself and
    # a doubled separator would reach the server as a different URL.
    assert ref.uri == "https://omnigraph.test"
    assert ref.graph_id == "code-github-com-mitodl-ol-django"
    assert ref.token == "tok"
    # The filesystem questions have an explicit answer, not a coincidental one.
    assert ref.local_path is None
    assert ref.stats() == (None, None)


def test_store_for_repo_stays_local_without_a_code_server(tmp_path, monkeypatch):
    from witan_code import config as cfg_module

    monkeypatch.delenv("WITAN_CODE_SERVER", raising=False)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path))
    cfg = cfg_module.load()

    ref = store_module.store_for_repo("https://github.com/test/cg", cfg)

    assert not ref.is_remote
    assert ref.local_path == tmp_path / "https_github.com_test_cg.omni"


def test_code_server_must_be_a_url(monkeypatch):
    """A store directory in `code_server` would resolve to `--store <path>` on
    every repo — every graph silently collapsed onto one local store."""
    import pytest

    from witan_code import config as cfg_module

    monkeypatch.setenv("WITAN_CODE_SERVER", "~/.local/share/witan/code")
    with pytest.raises(ValueError, match="http"):
        cfg_module.load()


def test_per_repo_stores_lists_the_clusters_code_graphs(monkeypatch):
    from witan_code import config as cfg_module

    _cluster(
        monkeypatch,
        "code-github-com-mitodl-ol-django",
        "code-github-com-mitodl-agent-kit",
        cfg_module.BRIDGE_GRAPH_ID,
        "council",  # witan's own memory graph, on the same server
    )
    cfg = cfg_module.load()

    ids = [r.graph_id for r in store_module.per_repo_stores(cfg)]

    # The bridge graph is listed separately (`bridge_store`) and witan's
    # memory graph is not a code graph at all.
    assert ids == [
        "code-github-com-mitodl-agent-kit",
        "code-github-com-mitodl-ol-django",
    ]


def test_ensure_store_names_the_graph_it_wanted_when_it_is_not_served(monkeypatch):
    """The error has to name the graph id it asked for — the usual cause is an
    id that drifted from provisioning's, which is invisible otherwise. It can
    no longer also list what the server DOES serve: that needs `graphs list`,
    the surface this path was rewritten to stop depending on."""
    from witan_code import config as cfg_module

    _cluster(monkeypatch, "code-github-com-mitodl-ol-django")
    cfg = cfg_module.load()

    with pytest.raises(store_module.ClusterGraphMissing) as exc:
        store_module.ensure_store("https://github.com/test/never-provisioned", cfg)

    message = str(exc.value)
    assert "code-github-com-test-never-provisioned" in message
    assert "data_tier.py" in message


def test_ensure_store_accepts_a_declared_graph(monkeypatch):
    from witan_code import config as cfg_module

    repo = "https://github.com/mitodl/ol-django"
    _cluster(monkeypatch, cfg_module.graph_id(repo))
    cfg = cfg_module.load()

    # No init, no schema apply — provisioning declares and applies both.
    assert store_module.ensure_store(repo, cfg).graph_id == cfg_module.graph_id(repo)


def test_bridge_store_uses_the_fixed_cluster_graph_id(monkeypatch):
    from witan_code import config as cfg_module

    _cluster(monkeypatch, cfg_module.BRIDGE_GRAPH_ID)
    cfg = cfg_module.load()

    assert store_module.ensure_bridge_store(cfg).graph_id == cfg_module.BRIDGE_GRAPH_ID


def test_repo_for_store_asks_a_cluster_graph_what_it_holds(monkeypatch):
    """`graph_id` does not invert, so the URI has to come from the graph.
    A graph with no files has no answer and falls back to its id."""
    from witan_code import config as cfg_module

    monkeypatch.setenv("WITAN_CODE_SERVER", "https://omnigraph.test")
    cfg = cfg_module.load()
    ref = store_module.store_for_repo("https://github.com/mitodl/ol-django", cfg)

    monkeypatch.setattr(
        store_module,
        "OmnigraphClient",
        lambda *a, **kw: _StubClient(
            [{"f.repo": "https://github.com/mitodl/ol-django"}]
        ),
    )
    assert (
        store_module.repo_for_store(ref, cfg) == "https://github.com/mitodl/ol-django"
    )

    monkeypatch.setattr(
        store_module, "OmnigraphClient", lambda *a, **kw: _StubClient([])
    )
    assert store_module.repo_for_store(ref, cfg) == "code-github-com-mitodl-ol-django"


# Captured verbatim from `GET /graphs` on the deployed CI omnigraph-server
# 0.8.1 (2026-08-06), trimmed to three of its seventeen graphs. Real rather
# than hand-written on purpose: every shape this parser used to accept was
# invented from CLI help, and none of them is what the server sends, so it
# would have returned [] against a perfectly good response.
REAL_GRAPHS_BODY = """
{"graphs": [
  {"graph_id": "code-bridge",
   "uri": "s3://ol-data-witan-ci/graphs/code-bridge.omni"},
  {"graph_id": "code-github-com-mitodl-agent-kit",
   "uri": "s3://ol-data-witan-ci/graphs/code-github-com-mitodl-agent-kit.omni"},
  {"graph_id": "council",
   "uri": "s3://ol-data-witan-ci/graphs/council.omni"}
]}
"""


def test_parse_graph_ids_reads_what_the_server_actually_sends():
    assert store_module._parse_graph_ids(REAL_GRAPHS_BODY) == [
        "code-bridge",
        "code-github-com-mitodl-agent-kit",
        "council",
    ]


def test_parse_graph_ids_reads_both_envelopes():
    assert store_module._parse_graph_ids('["a", "b"]') == ["a", "b"]
    assert store_module._parse_graph_ids('{"graphs": [{"graph_id": "a"}]}') == ["a"]
    assert store_module._parse_graph_ids('{"graphs": [{"id": "a"}]}') == ["a"]
    assert store_module._parse_graph_ids('{"graphs": [{"name": "b"}]}') == ["b"]
    assert store_module._parse_graph_ids("not json") == []


def test_parse_graph_ids_mixes_plain_ids_and_records_without_calling_get_on_a_str():
    """A plain-string row must never reach `.get`.

    The row-shape expression reads as though the `.get`s might be evaluated
    unconditionally (a review read it that way). They are not — `or` binds
    tighter than the conditional — and a mixed list is the case that would
    raise AttributeError if that were ever to change.
    """
    assert store_module._parse_graph_ids('["a", {"id": "b"}]') == ["a", "b"]
    # Non-string, non-dict rows are skipped rather than coerced.
    assert store_module._parse_graph_ids('["a", 7, null, {"id": "b"}]') == ["a", "b"]


class _FakeTransport:
    """Records what was asked for and replays a canned Outcome."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def graphs(self, token):
        self.calls.append(token)
        return self.outcome


def _install_transport(monkeypatch, outcome):
    fake = _FakeTransport(outcome)
    monkeypatch.setattr(store_module, "shared_transport", lambda url: fake)
    store_module.reset_graph_cache()
    return fake


def test_cluster_graphs_reads_the_http_listing(monkeypatch):
    fake = _install_transport(
        monkeypatch, http_module.Outcome(kind=http_module.OK, body=REAL_GRAPHS_BODY)
    )
    got = store_module.cluster_graphs("http://server:8080", "tok")
    assert got == frozenset(
        {"code-bridge", "code-github-com-mitodl-agent-kit", "council"}
    )
    # The server-scoped call authenticates as whoever the caller named.
    assert fake.calls == ["tok"]


def test_cluster_graphs_does_not_shell_out(monkeypatch):
    """REGRESSION: the CLI cannot answer this at all.

    `omnigraph graphs list --server <url>` fetches the listing and then refuses
    to print it against any multi-graph server, so a subprocess here is not a
    slower path — it is a guaranteed ClusterUnreachable, which is what silently
    emptied `code_indexed_repos` on the deployed tier.
    """
    _install_transport(
        monkeypatch, http_module.Outcome(kind=http_module.OK, body=REAL_GRAPHS_BODY)
    )
    monkeypatch.setattr(
        store_module.subprocess,
        "run",
        lambda *a, **kw: pytest.fail("cluster_graphs must not shell out"),
    )
    assert store_module.cluster_graphs("http://server:8080", "tok")


def test_cluster_graphs_raises_cluster_unreachable_on_a_failed_listing(monkeypatch):
    """An auth failure must not read as "the server has no graphs"."""
    _install_transport(
        monkeypatch,
        http_module.Outcome(
            kind=http_module.FATAL, error="invalid bearer token", status=401
        ),
    )
    with pytest.raises(store_module.ClusterUnreachable, match="invalid bearer token"):
        store_module.cluster_graphs("http://server:8080", "tok")


def test_safe_cluster_graphs_degrades_to_empty(monkeypatch):
    _install_transport(
        monkeypatch,
        http_module.Outcome(kind=http_module.UNAVAILABLE, error="connection refused"),
    )
    assert store_module.safe_cluster_graphs("http://server:8080", None) == frozenset()


def test_cluster_graphs_caches_success_but_not_failure(monkeypatch):
    ok = _install_transport(
        monkeypatch, http_module.Outcome(kind=http_module.OK, body='{"graphs": []}')
    )
    assert store_module.cluster_graphs("http://server:8080", None) == frozenset()
    assert store_module.cluster_graphs("http://server:8080", None) == frozenset()
    assert len(ok.calls) == 1, "an empty-but-successful listing is cached"

    bad = _FakeTransport(
        http_module.Outcome(kind=http_module.UNAVAILABLE, error="down")
    )
    monkeypatch.setattr(store_module, "shared_transport", lambda url: bad)
    store_module.reset_graph_cache()
    for _ in range(2):
        with pytest.raises(store_module.ClusterUnreachable):
            store_module.cluster_graphs("http://server:8080", None)
    assert len(bad.calls) == 2, "a failure must not pin an error for the whole TTL"


def test_cluster_graphs_cache_does_not_span_credentials(monkeypatch):
    """A success under one credential must not answer for a different one.

    The listing is a function of (server, token) — an unusable token RAISES
    where a good one returns — so a cache keyed on the server alone would let
    one success make every later credential look valid for the rest of the TTL.
    Not reachable from today's single call site, which passes the process-wide
    `cfg.code_token` every time; pinned because the bug only becomes visible
    once that changes, and it would look like an auth bypass when it did.
    """
    transports = {
        "good": _FakeTransport(
            http_module.Outcome(kind=http_module.OK, body=REAL_GRAPHS_BODY)
        ),
        "bad": _FakeTransport(
            http_module.Outcome(
                kind=http_module.FATAL, error="invalid bearer token", status=401
            )
        ),
    }
    # One transport per server, as in production — the credential is a per-call
    # argument, so only the cache key can tell the two calls apart.
    current = {"token": "good"}
    monkeypatch.setattr(
        store_module, "shared_transport", lambda url: transports[current["token"]]
    )
    store_module.reset_graph_cache()

    assert store_module.cluster_graphs("http://server:8080", "good")

    current["token"] = "bad"
    with pytest.raises(store_module.ClusterUnreachable, match="invalid bearer token"):
        store_module.cluster_graphs("http://server:8080", "bad")

    # ...and the good credential is still served from cache, so the tighter key
    # did not simply disable caching.
    current["token"] = "good"
    assert store_module.cluster_graphs("http://server:8080", "good")
    assert len(transports["good"].calls) == 1


def test_graph_cache_key_does_not_retain_the_raw_token():
    """The cache is process-global and long-lived; a bearer token has no
    business sitting in one as a plain string."""
    key = store_module._cache_key("http://server:8080", "s3cret-token-value")
    assert "s3cret-token-value" not in repr(key)
    assert store_module._cache_key("http://server:8080", None) == (
        "http://server:8080",
        None,
    )


# ── map_refs ──────────────────────────────────────────────────────────────────
#
# The per-graph listing questions are one round trip each against S3-backed
# graphs (~150-350 ms), so `code_indexed_repos` measured 5.7 s across 14 CI
# graphs before this. These pin the two properties that make concurrency safe
# here: order, and that it actually runs concurrently.


def _refs(n):
    return [store_module.StoreRef("https://srv", f"code-g{i}") for i in range(n)]


def test_map_refs_preserves_order_regardless_of_completion_order():
    """THE reason this is not `_fan_out`.

    Callers zip results back against `refs` positionally, so a completion-order
    result would attribute one graph's file count to another — a wrong answer,
    not a slow one.

    Completion order is inverted by a chain of Events rather than by staggered
    sleeps: each task waits for its successor to finish before finishing
    itself, so the last ref provably completes first and the first completes
    last. Sleeps would only make that ordering *likely* — and a run where they
    landed in order would silently PASS against an `as_completed`
    implementation, which is the one thing this test exists to catch.
    """
    refs = _refs(4)
    done = [threading.Event() for _ in refs]

    def finish_in_reverse(ref):
        index = int(ref.graph_id.removeprefix("code-g"))
        if index + 1 < len(refs):
            assert done[index + 1].wait(timeout=5), "successor never finished"
        done[index].set()
        return ref.graph_id

    assert store_module.map_refs(refs, finish_in_reverse, max_workers=len(refs)) == [
        r.graph_id for r in refs
    ]


def test_map_refs_actually_runs_concurrently():
    """A serial implementation passes every other test here, so pin the one
    property they cannot: that the work genuinely overlaps.

    A rendezvous rather than a stopwatch. ``Barrier(len(refs))`` clears only
    when every task is inside ``fn`` at the same instant, which IS the claim —
    where an elapsed-time threshold only infers it, and infers it from a number
    tuned on an idle machine. A serial implementation never gets a second task
    to the barrier and fails on its timeout; a slow-but-concurrent CI runner
    still passes, because arriving late is not arriving alone.

    ``max_workers`` is passed explicitly so the parties count cannot drift away
    from the pool width if the default cap changes — the cap has its own test.
    """
    refs = _refs(4)
    barrier = threading.Barrier(len(refs), timeout=5)

    def rendezvous(_ref):
        barrier.wait()
        return True

    assert store_module.map_refs(refs, rendezvous, max_workers=len(refs)) == [
        True
    ] * len(refs)


def test_map_refs_caps_width_and_does_not_over_subscribe():
    """Capped at 8 to match `_fan_out`. 20 refs must not open 20 connections."""
    live = []
    peak = 0
    lock = threading.Lock()

    def track(_ref):
        nonlocal peak
        with lock:
            live.append(1)
            peak = max(peak, len(live))
        time.sleep(0.02)
        with lock:
            live.pop()

    store_module.map_refs(_refs(20), track)
    assert peak <= 8, f"peak concurrency {peak} exceeded the cap"


def test_map_refs_skips_the_pool_for_zero_or_one_ref():
    """The single-repo case is the common one locally; a thread pool for one
    item is pure overhead."""
    assert store_module.map_refs([], lambda _r: pytest.fail("should not run")) == []

    calling_threads = []
    store_module.map_refs(
        _refs(1), lambda _r: calling_threads.append(threading.current_thread())
    )
    assert calling_threads == [threading.current_thread()], "ran off the main thread"


def test_map_refs_propagates_an_exception():
    """Every current `fn` degrades internally, so a raise here is something the
    caller should not paper over."""

    def boom(ref):
        if ref.graph_id == "code-g2":
            raise RuntimeError("graph exploded")
        return ref.graph_id

    with pytest.raises(RuntimeError, match="graph exploded"):
        store_module.map_refs(_refs(4), boom)
