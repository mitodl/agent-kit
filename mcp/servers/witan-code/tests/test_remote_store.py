"""The client half of writing a code graph through the MCP tier (ADR-0005 c).

``RemoteStoreClient`` stands in for an ``OmnigraphClient``, so the strongest
statement these can make is the end-to-end one: run the *real*
``indexer.index_path`` with the transport switched, against an in-memory server
holding a real store, and check the symbols landed. That exercises the whole
chain — store resolution, view creation, the incremental hash read, the bulk
load, and the server-side ownership guard — without a network or a cluster.

The rest pin the transport's own behavior: a held-open connection, one
reconnect on a dropped one, and refusals for the operations that belong to the
cluster rather than to a client of it.
"""

from __future__ import annotations

import subprocess

import pytest
from fastmcp import Client

from witan_code import config as cfg_module
from witan_code import store as store_module
from witan_code.remote.store import (
    RemotePayloadTooLarge,
    RemoteWriteIndeterminate,
    RemoteStoreClient,
    RemoteStoreUnsupported,
    StoreSession,
)

from .conftest import SAMPLE, requires_stack

REPO = "https://github.com/test/cg"
ACTOR = "act-alice"


def _git(base, *args):
    subprocess.run(
        ["git", "-C", str(base), *args], check=True, capture_output=True, text=True
    )


def _git_repo(path, branch="main"):
    path.mkdir(exist_ok=True)
    _git(path, "init", "-q", "-b", branch)
    _git(
        path,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "--allow-empty",
        "-q",
        "-m",
        "init",
    )
    return path


@pytest.fixture
def in_memory_tier(tmp_path, monkeypatch):
    """A deployment serving the store tools over an in-memory MCP connection.

    Client and server share one process here, so they would otherwise share one
    configuration — and a server that agreed with the client about the
    transport would proxy to itself. The server's config is captured while the
    environment still describes local stores, and pinned through the seam
    ``ingest._config`` exists for.
    """
    from witan_code import ingest
    from witan_code import server as srv

    monkeypatch.setenv("WITAN_REPO", REPO)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    server_cfg = cfg_module.load()
    monkeypatch.setattr(ingest, "_config", lambda: server_cfg)
    monkeypatch.setattr(srv, "cfg", server_cfg)
    srv._clients.clear()
    srv._git_context.clear()
    srv.register_store_tools()

    # Now the client's own view: the graphs are the deployment's, reached
    # through it. `WITAN_ACTOR` stands in for the OIDC session `witan login`
    # would have established.
    monkeypatch.setenv("WITAN_CODE_TRANSPORT", cfg_module.CODE_TRANSPORT_MCP)
    monkeypatch.setenv("WITAN_REMOTE_URL", "http://in-memory/mcp")
    monkeypatch.setenv("WITAN_OIDC_ISSUER", "https://sso.example.org/realms/ol")
    monkeypatch.setenv("WITAN_ACTOR", ACTOR)

    session = StoreSession(
        "http://in-memory/mcp", lambda: "jwt", client_factory=lambda _t: Client(srv.mcp)
    )
    monkeypatch.setattr(store_module, "mcp_session", lambda *_a, **_kw: session)
    yield session, server_cfg
    session.close()


# ── End to end ────────────────────────────────────────────────────────────────


@requires_stack
def test_a_branch_index_lands_on_the_deployments_store(in_memory_tier, tmp_path):
    """The deliverable: a checkout outside the cluster indexes onto a graph it
    cannot address directly, into the view it owns."""
    from witan_code import indexer
    from witan_code.graph import OmnigraphClient

    _session, server_cfg = in_memory_tier
    base = _git_repo(tmp_path / "r")
    (base / "svc.py").write_text(SAMPLE)
    _git(base, "checkout", "-q", "-b", "feature/new-api")

    stats = indexer.index_path(base, config=cfg_module.load())
    assert stats.indexed >= 1
    assert stats.errors == 0

    # Read the deployment's own store directly — the write went all the way
    # through, not into a local store the client kept for itself.
    store = str(cfg_module.store_path(REPO, server_cfg.code_dir))
    view = f"{ACTOR}/feature_new-api"
    client = OmnigraphClient(store, server_cfg.queries_dir, branch=view)
    files = {row["slug"] for row in client.read("code_read.gq", "all_file_hashes", {})}
    assert f"{REPO}#svc.py" in files
    # The shared default view is untouched: nothing indexed it.
    main = OmnigraphClient(store, server_cfg.queries_dir)
    assert main.read("code_read.gq", "all_file_hashes", {}) == []


@requires_stack
def test_a_view_owned_by_someone_else_is_refused_by_the_server(in_memory_tier):
    """The guard that matters is the server's: a client that names another
    actor's view is refused even though its own check would have passed."""
    session, _cfg = in_memory_tier
    client = RemoteStoreClient(REPO, session, branch="act-bob/feature_x")
    with pytest.raises(Exception, match="owned by act-bob"):
        client.load([{"type": "CodeFile", "data": {"slug": "x"}}])


@requires_stack
def test_the_default_view_is_not_writable_through_the_tier(in_memory_tier):
    """CI owns it, and CI writes in-cluster — so no request through this
    boundary may claim it, whoever is asking."""
    session, _cfg = in_memory_tier
    client = RemoteStoreClient(REPO, session)
    with pytest.raises(Exception, match="owned by CI"):
        client.load([{"type": "CodeFile", "data": {"slug": "x"}}])


@requires_stack
def test_listing_graphs_answers_in_repo_uris(in_memory_tier, tmp_path):
    from witan_code import indexer

    _session, server_cfg = in_memory_tier
    base = _git_repo(tmp_path / "r")
    (base / "svc.py").write_text(SAMPLE)
    _git(base, "checkout", "-q", "-b", "feature/x")
    indexer.index_path(base, config=cfg_module.load())

    assert [ref.via_mcp for ref in store_module.per_repo_stores(cfg_module.load())] == [
        REPO
    ]


# ── Store resolution ──────────────────────────────────────────────────────────


def test_the_transport_decides_the_address(monkeypatch, tmp_path):
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setenv("WITAN_CODE_TRANSPORT", cfg_module.CODE_TRANSPORT_MCP)
    monkeypatch.setenv("WITAN_REMOTE_URL", "https://witan.example.org/mcp")
    monkeypatch.setenv("WITAN_OIDC_ISSUER", "https://sso.example.org/realms/ol")

    ref = store_module.store_for_repo(REPO, cfg_module.load())
    assert ref.via_mcp == REPO
    assert ref.uri == "https://witan.example.org/mcp"
    # Shared by construction — which is what the write guard reads.
    assert ref.is_remote is True
    assert ref.local_path is None
    assert ref.stats() == (None, None)

    bridge = store_module.bridge_store(cfg_module.load())
    assert bridge.via_mcp == cfg_module.BRIDGE_GRAPH_ID


def test_the_endpoint_comes_from_the_config_that_asked_for_it(monkeypatch, tmp_path):
    """A `Config` from an explicit `load(target=…)` must resolve *that*
    target's endpoint. Re-running target selection would fall back to
    auto-detection and could answer with a different deployment than the one
    whose `code_transport` sent the write here."""
    config = tmp_path / "config.toml"
    config.write_text(
        'remote_url = "https://global.example.org/mcp"\n'
        'oidc_issuer = "https://sso.example.org/realms/global"\n'
        "\n"
        "[targets.hosted]\n"
        'remote_url = "https://hosted.example.org/mcp"\n'
        'oidc_issuer = "https://sso.example.org/realms/hosted"\n'
        'code_transport = "mcp"\n'
        'match_orgs = ["never-matches-this-checkout"]\n'
    )
    monkeypatch.setenv("WITAN_CONFIG", str(config))
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.delenv("WITAN_REMOTE_URL", raising=False)
    monkeypatch.delenv("WITAN_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("WITAN_CODE_TRANSPORT", raising=False)

    cfg = cfg_module.load(target="hosted")
    assert (
        store_module.store_for_repo(REPO, cfg).uri == "https://hosted.example.org/mcp"
    )


def test_no_endpoint_is_a_configuration_error_not_a_local_store(monkeypatch, tmp_path):
    """Degrading to a local store would index into a directory nobody reads."""
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setenv("WITAN_CODE_TRANSPORT", cfg_module.CODE_TRANSPORT_MCP)
    monkeypatch.delenv("WITAN_REMOTE_URL", raising=False)
    monkeypatch.setenv("WITAN_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.delenv("WITAN_TARGET", raising=False)

    with pytest.raises(ValueError, match="no.*configured"):
        store_module.store_for_repo(REPO, cfg_module.load())


def test_an_unknown_transport_is_rejected_at_load(monkeypatch, tmp_path):
    monkeypatch.setenv("WITAN_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.setenv("WITAN_CODE_TRANSPORT", "http")
    with pytest.raises(ValueError, match="Unknown code_transport"):
        cfg_module.load()


# ── The transport itself ──────────────────────────────────────────────────────


class _Tool:
    def __init__(self, name):
        self.name = name


class _FakeClient:
    """Stands in for a connected MCP client; records the calls it served.

    ``tools`` is the surface the deployment advertises. It defaults to the full
    current one; a test pins an OLDER deployment by passing a narrower set.
    """

    DEFAULT_TOOLS = (
        "code_store_read",
        "code_store_mutate",
        "code_store_mutate_many",
        "code_store_load",
        "code_store_open",
        "code_store_views",
    )

    def __init__(self, log, fail_first=False, tools=None):
        self._log = log
        self._fail_first = fail_first
        self._tools = self.DEFAULT_TOOLS if tools is None else tuple(tools)

    async def __aenter__(self):
        self._log.append(("connect", None))
        return self

    async def __aexit__(self, *_exc):
        self._log.append(("disconnect", None))

    async def list_tools(self):
        self._log.append(("list_tools", None))
        return [_Tool(name) for name in self._tools]

    async def call_tool(self, name, arguments):
        if self._fail_first:
            self._fail_first = False
            raise ConnectionError("peer went away")
        self._log.append((name, arguments))

        class _Result:
            data = ["main"]

        return _Result()


def test_the_connection_is_held_open_across_calls():
    """One index is thousands of store calls; a handshake each would dominate."""
    log: list = []
    session = StoreSession("http://x/mcp", lambda: "jwt", lambda _t: _FakeClient(log))
    client = RemoteStoreClient(REPO, session)
    client.list_branches()
    client.list_branches()
    session.close()
    assert [entry[0] for entry in log] == [
        "connect",
        "code_store_views",
        "code_store_views",
        "disconnect",
    ]


def test_a_dropped_connection_is_reconnected_once():
    """A long index must not lose its remaining writes to one dead socket."""
    log: list = []
    clients = iter([_FakeClient(log, fail_first=True), _FakeClient(log)])
    session = StoreSession("http://x/mcp", lambda: "jwt", lambda _t: next(clients))
    assert RemoteStoreClient(REPO, session).list_branches() == ["main"]
    assert [entry[0] for entry in log] == [
        "connect",
        "disconnect",
        "connect",
        "code_store_views",
    ]
    session.close()


class _RefusesForSize(_FakeClient):
    """A deployment that answers every call with a size refusal.

    ``exc`` is the shape under test: the two faces a 413 wears on this path.
    """

    def __init__(self, log, exc):
        super().__init__(log)
        self._exc = exc

    async def call_tool(self, name, arguments):
        self._log.append(("attempt", name))
        raise self._exc


def _http_413():
    import httpx2

    request = httpx2.Request("POST", "http://x/mcp")
    response = httpx2.Response(413, request=request, text="Request body too large")
    return httpx2.HTTPStatusError("413", request=request, response=response)


def _relayed_413():
    from fastmcp.exceptions import ToolError

    return ToolError(
        'backend unavailable: tool call failed on backend witan: calling "tools/call": '
        "Request Entity Too Large: request failed with status 413: Request body too large"
    )


@pytest.mark.parametrize(
    "exc", [_http_413(), _relayed_413()], ids=["direct", "relayed"]
)
def test_an_oversized_batch_is_refused_once_and_never_retried(exc):
    """★ The retry is the bug, not just the traceback.

    A direct 413 is an httpx error, so the generic arm treats it as a dropped
    socket: reconnect and send the same multi-MiB body again, to be refused
    identically. That is one wasted round trip per batch of a repo-scale index
    before the run dies — and it died as a traceback either way.
    """
    log: list = []
    clients = iter([_RefusesForSize(log, exc), _RefusesForSize(log, exc)])
    session = StoreSession("http://x/mcp", lambda: "jwt", lambda _t: next(clients))
    with pytest.raises(RemotePayloadTooLarge) as caught:
        RemoteStoreClient(REPO, session).load([{"id": "n1", "type": "Symbol"}])
    # Exactly one attempt: no reconnect, no second send of the same payload.
    assert [entry[0] for entry in log] == ["connect", "attempt"]
    message = str(caught.value)
    assert "`code_store_load`" in message
    assert "http://x/mcp" in message
    # Chunked-merge context, added by `_load_refusal` where it is actually true.
    assert "budget of 2 MiB" in message
    assert "first batch" in message  # nothing applied — this WAS batch 1


def test_a_read_refusal_claims_nothing_about_batching_or_partial_indexes():
    """★ Every store call comes through one helper — most are not chunked loads.

    Reads, `code_store_views`, and single mutations carry no byte budget and
    apply nothing incrementally. An earlier revision told all of them that
    "records are already batched under 2 MiB" and that "the index stopped
    part-way", which was false for each.
    """
    log: list = []
    session = StoreSession(
        "http://x/mcp", lambda: "jwt", lambda _t: _RefusesForSize(log, _relayed_413())
    )
    with pytest.raises(RemotePayloadTooLarge) as caught:
        RemoteStoreClient(REPO, session).list_branches()
    message = str(caught.value).lower()
    for forbidden in ("batch", "part-way", "mib", "budget", "applied"):
        assert forbidden not in message, (
            f"a read refusal must not mention {forbidden!r}"
        )


def test_an_overwrite_load_is_not_told_to_shrink_a_budget_it_does_not_have():
    """`overwrite` is sent WHOLE — it cannot be chunked, so quoting a batch
    budget would point at a knob that does not apply to it."""
    log: list = []
    session = StoreSession(
        "http://x/mcp", lambda: "jwt", lambda _t: _RefusesForSize(log, _relayed_413())
    )
    with pytest.raises(RemotePayloadTooLarge) as caught:
        RemoteStoreClient(REPO, session).load(
            [{"id": "n1", "type": "Symbol"}], mode="overwrite"
        )
    message = str(caught.value)
    assert "sent whole" in message
    assert "cannot be chunked" in message
    assert "MiB" not in message  # no budget to quote
    assert "batch " not in message.lower()


def test_a_custom_max_bytes_is_reported_instead_of_the_default():
    """`load` takes `max_bytes` from its caller, so a hardcoded "2 MiB" would
    send the reader looking for a limit that is not the one that refused them."""
    log: list = []
    session = StoreSession(
        "http://x/mcp", lambda: "jwt", lambda _t: _RefusesForSize(log, _relayed_413())
    )
    with pytest.raises(RemotePayloadTooLarge) as caught:
        RemoteStoreClient(REPO, session).load(
            [{"id": "n1", "type": "Symbol"}], max_bytes=1_500_000
        )
    message = str(caught.value)
    assert "budget of 1,500,000 bytes" in message
    assert "2 MiB" not in message


def test_a_change_many_refusal_points_at_chunk_size_not_a_byte_budget():
    """`change_many` chunks by statement COUNT. There is no byte budget bounding
    it, so naming one would point at a knob that does not exist here."""
    log: list = []
    session = StoreSession(
        "http://x/mcp", lambda: "jwt", lambda _t: _RefusesForSize(log, _relayed_413())
    )
    steps = [("q.gq", "delete_file", {"id": str(n)}) for n in range(10)]
    with pytest.raises(RemotePayloadTooLarge) as caught:
        RemoteStoreClient(REPO, session).change_many(steps, chunk_size=4)
    message = str(caught.value)
    assert "4 statements" in message
    assert "`chunk_size`" in message
    assert "statement COUNT" in message
    assert "MiB" not in message  # no byte budget bounds this path
    assert "first chunk" in message  # nothing applied yet


def test_a_dropped_socket_is_still_retried_after_the_size_guard():
    """The guard sits ahead of the reconnect; it must not disable it."""
    log: list = []
    clients = iter([_FakeClient(log, fail_first=True), _FakeClient(log)])
    session = StoreSession("http://x/mcp", lambda: "jwt", lambda _t: next(clients))
    assert RemoteStoreClient(REPO, session).list_branches() == ["main"]
    assert [entry[0] for entry in log].count("connect") == 2
    session.close()


def test_an_ordinary_tool_failure_is_not_read_as_a_size_refusal():
    """A tool error relays the server's text, which can quote the caller's data."""
    from fastmcp.exceptions import ToolError
    from witan_code.remote.store import RemoteToolFailed

    log: list = []
    session = StoreSession(
        "http://x/mcp",
        lambda: "jwt",
        lambda _t: _RefusesForSize(log, ToolError("no view named 'payload-413'")),
    )
    with pytest.raises(RemoteToolFailed) as caught:
        RemoteStoreClient(REPO, session).list_branches()
    assert not isinstance(caught.value, RemotePayloadTooLarge)


def test_a_refused_store_call_reads_as_a_sentence_not_a_traceback():
    """This session is witan-code's OTHER transport, and it needs the same fix.

    Index and write commands reach the deployment through `StoreRef.client()`
    and this session, not through `RemoteMCPProxy` — so classifying refusals in
    the proxy alone left every one of them escaping as a raw `ToolError`, which
    is not a `RuntimeError` and so slips past `cli()`'s guard tuple.
    """
    from fastmcp.exceptions import ToolError
    from witan_code.remote.store import RemoteToolFailed

    refusal = ToolError("cedar: read denied on graph 'code'")
    session = StoreSession(
        "http://x/mcp", lambda: "jwt", lambda _t: _RefusesForSize([], refusal)
    )
    with pytest.raises(RemoteToolFailed, match="cedar: read denied") as caught:
        RemoteStoreClient(REPO, session).list_branches()
    assert isinstance(caught.value, RuntimeError)
    assert caught.value.__cause__ is refusal


def test_a_refusal_on_the_post_reconnect_retry_is_classified_too():
    """A refusal that happens to follow a dropped socket takes the other arm.

    The generic arm reconnects and re-invokes; classifying only the first
    attempt would let exactly the same refusal escape as a traceback whenever
    it landed after a dead connection rather than before one.
    """
    from fastmcp.exceptions import ToolError
    from witan_code.remote.store import RemoteToolFailed

    log: list = []
    clients = iter(
        [
            _FakeClient(log, fail_first=True),
            _RefusesForSize(log, ToolError("no view named 'branches'")),
        ]
    )
    session = StoreSession("http://x/mcp", lambda: "jwt", lambda _t: next(clients))
    with pytest.raises(RemoteToolFailed, match="no view named 'branches'"):
        RemoteStoreClient(REPO, session).list_branches()


def test_an_empty_load_costs_nothing():
    log: list = []
    session = StoreSession("http://x/mcp", lambda: "jwt", lambda _t: _FakeClient(log))
    RemoteStoreClient(REPO, session).load([])
    assert log == []


def test_the_view_travels_with_every_operation():
    log: list = []
    session = StoreSession("http://x/mcp", lambda: "jwt", lambda _t: _FakeClient(log))
    client = RemoteStoreClient(REPO, session, branch="act-alice/feature_x")
    client.read("code_read.gq", "all_file_hashes", {})
    client.change("delete.gq", "delete_file", {"id": "x"})
    client.ensure_branch()
    session.close()
    assert [entry[0] for entry in log if entry[1] is not None] == [
        "code_store_read",
        "code_store_mutate",
        "code_store_open",
    ]
    assert all(
        args.get("view") == "act-alice/feature_x"
        for _name, args in log
        if isinstance(args, dict)
    )


def test_a_branchless_client_has_no_view_to_open():
    log: list = []
    session = StoreSession("http://x/mcp", lambda: "jwt", lambda _t: _FakeClient(log))
    RemoteStoreClient(REPO, session).ensure_branch()
    assert log == []


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.optimize(),
        lambda c: c.cleanup(keep=1),
        lambda c: c.delete_branch("act-alice/x"),
        lambda c: c.branch_last_write("act-alice/x"),
    ],
)
def test_cluster_side_operations_are_refused_not_faked(call):
    """Compaction and reaping run against the storage root from inside the
    cluster, not from whichever laptop indexed last."""
    session = StoreSession("http://x/mcp", lambda: "jwt", lambda _t: _FakeClient([]))
    with pytest.raises(RemoteStoreUnsupported):
        call(RemoteStoreClient(REPO, session))


def test_load_chunks_against_the_mcp_budget_not_omnigraph_s():
    """Regression: records ride as a JSON tool parameter, so the SDK cap binds.

    `load` defaulted to `LOAD_MAX_BYTES` — omnigraph's 8 MiB buffered-body
    budget — while this transport's real ceiling is the MCP Python SDK's 4 MiB
    request cap. A large enough index would have failed here exactly as `witan
    migrate merge` did against the deployment: `413 Request body too large`,
    from the SDK rather than from omnigraph.

    Asserts on the request bodies these batches actually become, not on the
    batch count, so the JSONL-vs-envelope accounting difference cannot quietly
    eat the margin.
    """
    import json

    from mcp.server.streamable_http_manager import DEFAULT_MAX_REQUEST_BODY_SIZE

    log: list = []
    session = StoreSession("http://x/mcp", lambda: "jwt", lambda _t: _FakeClient(log))

    # ~6 MiB of records: over the SDK cap, under omnigraph's 8 MiB budget, so
    # the old default produced a single request the deployment would refuse.
    records = [
        {"type": "Symbol", "data": {"slug": f"sym-{i}", "body": "x" * 4096}}
        for i in range(1500)
    ]
    RemoteStoreClient(REPO, session).load(records)

    loads = [args for name, args in log if name == "code_store_load"]
    assert len(loads) > 1, "should have split; a single call is the old bug"

    for args in loads:
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "code_store_load", "arguments": args},
            }
        )
        assert len(body.encode()) < DEFAULT_MAX_REQUEST_BODY_SIZE

    # Nothing dropped on the way through the splitter.
    assert sum(len(args["records"]) for args in loads) == len(records)


# ── Batched mutation ──────────────────────────────────────────────────────────
#
# `change_many` was the last per-row writer in witan-code: every other write
# path collapses N rows into one commit, and this one looped `change`. What
# these pin is the COUNT — one call and one commit per chunk, not per step —
# because a reindex's cost is exactly that count.


def _steps(n):
    return [("delete.gq", "delete_file", {"id": f"f{i}"}) for i in range(n)]


def _calls(log, name):
    return [args for entry, args in log if entry == name]


def test_a_batch_is_one_call_carrying_every_step():
    """The deliverable. 400 deletes used to be 400 round trips and 400 Lance
    versions; they are now one of each."""
    log: list = []
    session = StoreSession("http://x/mcp", lambda: "jwt", lambda _t: _FakeClient(log))
    RemoteStoreClient(REPO, session, branch="act-alice/x").change_many(_steps(400))
    session.close()

    batched = _calls(log, "code_store_mutate_many")
    assert len(batched) == 1
    assert _calls(log, "code_store_mutate") == []
    assert batched[0]["graph"] == REPO
    assert batched[0]["view"] == "act-alice/x"
    assert len(batched[0]["steps"]) == 400
    # The wire form carries params, never composed GQ — that is what keeps the
    # tier's surface the named queries Cedar already scopes.
    assert batched[0]["steps"][0] == {
        "query": "delete.gq",
        "name": "delete_file",
        "params": {"id": "f0"},
    }


def test_the_steps_keep_their_order_over_the_wire():
    """An edge statement may reference a node an earlier step inserted."""
    log: list = []
    session = StoreSession("http://x/mcp", lambda: "jwt", lambda _t: _FakeClient(log))
    RemoteStoreClient(REPO, session).change_many(_steps(5))
    session.close()
    sent = _calls(log, "code_store_mutate_many")[0]["steps"]
    assert [step["params"]["id"] for step in sent] == ["f0", "f1", "f2", "f3", "f4"]


def test_chunk_size_still_means_statements_per_commit():
    """Commit granularity must not depend on which transport is in use, or a
    partial failure means something different remotely than it does locally."""
    log: list = []
    session = StoreSession("http://x/mcp", lambda: "jwt", lambda _t: _FakeClient(log))
    RemoteStoreClient(REPO, session).change_many(_steps(250), chunk_size=100)
    session.close()

    batched = _calls(log, "code_store_mutate_many")
    assert [len(call["steps"]) for call in batched] == [100, 100, 50]
    # Every step is sent exactly once; the chunking drops none and repeats none.
    assert [step["params"]["id"] for call in batched for step in call["steps"]] == [
        f"f{i}" for i in range(250)
    ]


def test_a_chunk_size_below_one_is_refused_rather_than_dropping_every_write():
    """`range(0, n, 0)` raises, but `range(0, n, -1)` is EMPTY — the second
    would return normally having written nothing at all."""
    session = StoreSession("http://x/mcp", lambda: "jwt", lambda _t: _FakeClient([]))
    for size in (0, -1):
        with pytest.raises(ValueError, match="chunk_size must be >= 1"):
            RemoteStoreClient(REPO, session).change_many(_steps(3), chunk_size=size)


def test_an_empty_batch_costs_nothing():
    log: list = []
    session = StoreSession("http://x/mcp", lambda: "jwt", lambda _t: _FakeClient(log))
    RemoteStoreClient(REPO, session).change_many([])
    assert log == []


def test_a_deployment_without_the_batch_tool_gets_the_per_step_loop():
    """This is a DEPLOYED contract: the server ships before its clients, but a
    client can still meet an older one. It must be slow, not broken."""
    log: list = []
    older = [t for t in _FakeClient.DEFAULT_TOOLS if t != "code_store_mutate_many"]
    session = StoreSession(
        "http://x/mcp", lambda: "jwt", lambda _t: _FakeClient(log, tools=older)
    )
    RemoteStoreClient(REPO, session, branch="act-alice/x").change_many(_steps(3))
    session.close()

    assert _calls(log, "code_store_mutate_many") == []
    assert [c["params"]["id"] for c in _calls(log, "code_store_mutate")] == [
        "f0",
        "f1",
        "f2",
    ]


class _Unaskable(_FakeClient):
    """A deployment too old to answer `list_tools` at all."""

    def __init__(self, log, **kw):
        super().__init__(log, **kw)
        self.asked = 0

    async def list_tools(self):
        self.asked += 1
        raise RuntimeError("no such method")


def test_a_server_that_cannot_be_asked_falls_back_rather_than_failing():
    """A deployment too old to answer `list_tools` is certainly too old to have
    the batch tool, and refusing here would fail an index that can succeed."""
    log: list = []
    session = StoreSession("http://x/mcp", lambda: "jwt", lambda _t: _Unaskable(log))
    RemoteStoreClient(REPO, session).change_many(_steps(2))
    session.close()
    assert len(_calls(log, "code_store_mutate")) == 2


def test_a_failed_tool_listing_is_cached_like_a_successful_one():
    """Otherwise an index re-asks a server that already refused once, paying a
    round trip AND an exception on every batch — the per-call cost this whole
    change exists to remove."""
    log: list = []
    old = _Unaskable(log)
    session = StoreSession("http://x/mcp", lambda: "jwt", lambda _t: old)
    client = RemoteStoreClient(REPO, session)
    for _ in range(5):
        client.change_many(_steps(1))
    session.close()

    assert old.asked == 1, f"re-asked {old.asked} times; should ask once per connection"
    assert len(_calls(log, "code_store_mutate")) == 5  # all five still written


def test_a_reconnect_clears_a_cached_failure_too():
    """The cache dies with the connection, so a transport blip costs the slow
    path only until the next reconnect — not for the life of the process."""
    log: list = []
    old = _Unaskable(log)
    session = StoreSession("http://x/mcp", lambda: "jwt", lambda _t: old)
    client = RemoteStoreClient(REPO, session)
    client.change_many(_steps(1))
    assert old.asked == 1
    session._disconnect()
    client.change_many(_steps(1))
    session.close()
    assert old.asked == 2


def test_the_tool_surface_is_asked_once_per_connection_not_once_per_batch():
    """An index issues many batches; re-listing the surface each time would put
    back the round trip batching just removed."""
    log: list = []
    session = StoreSession("http://x/mcp", lambda: "jwt", lambda _t: _FakeClient(log))
    client = RemoteStoreClient(REPO, session)
    client.change_many(_steps(2))
    client.change_many(_steps(2))
    client.change_many(_steps(2))
    session.close()
    assert [entry for entry, _ in log].count("list_tools") == 1


def test_a_reconnect_re_asks_the_surface():
    """A dropped connection may reconnect to a different replica mid-rollout,
    so the cached surface must not outlive the connection it describes."""
    log: list = []
    clients = iter([_FakeClient(log, fail_first=True), _FakeClient(log)])
    session = StoreSession("http://x/mcp", lambda: "jwt", lambda _t: next(clients))
    client = RemoteStoreClient(REPO, session)
    client.change_many(_steps(1))
    client.change_many(_steps(1))
    session.close()
    assert [entry for entry, _ in log].count("list_tools") == 2


def _gateway(status: int):
    import httpx2

    request = httpx2.Request("POST", "http://x/mcp")
    response = httpx2.Response(status, request=request, text="<html>502 Bad Gateway")
    return httpx2.HTTPStatusError(str(status), request=request, response=response)


@pytest.mark.parametrize("status", [502, 504])
def test_a_write_cut_off_by_a_gateway_is_never_re_invoked(status):
    """★ THE RETRY IS THE BUG, and here it duplicates data rather than wasting a
    round trip.

    A 502/504 is not a ``ToolError``, so it lands in the generic arm — which
    reconnects and sends the write AGAIN. Measured against CI, the backend
    usually finished the write before the deadline cut the response (28 of 28
    committed in one burst, 14 of 16 in another), so that second invocation
    writes the rows a second time. The outcome is indeterminate, not failed, and
    the only safe response is to stop and say so.
    """
    log: list = []
    exc = _gateway(status)
    clients = iter([_RefusesForSize(log, exc), _RefusesForSize(log, exc)])
    session = StoreSession("http://x/mcp", lambda: "jwt", lambda _t: next(clients))
    with pytest.raises(RemoteWriteIndeterminate) as caught:
        RemoteStoreClient(REPO, session).load([{"id": "n1", "type": "Symbol"}])
    # Exactly one attempt: the reconnect-and-retry must not have happened.
    assert [entry[0] for entry in log] == ["connect", "attempt"]
    message = str(caught.value)
    assert "INDETERMINATE" in message
    assert "NOT retried" in message
    assert f"HTTP {status}" in message
    assert "`code_store_load`" in message


def test_a_read_cut_off_by_a_gateway_still_reconnects():
    """The other half: repeating a read changes nothing, so a dropped session is
    exactly what the reconnect is for. Refusing it would be a strict loss."""
    log: list = []

    class _FailsOnceWithGateway(_FakeClient):
        def __init__(self, log, fail):
            super().__init__(log)
            self._fail = fail

        async def call_tool(self, name, arguments):
            if self._fail:
                self._log.append(("attempt", name))
                raise _gateway(502)
            return await super().call_tool(name, arguments)

    clients = iter(
        [_FailsOnceWithGateway(log, True), _FailsOnceWithGateway(log, False)]
    )
    session = StoreSession("http://x/mcp", lambda: "jwt", lambda _t: next(clients))
    assert RemoteStoreClient(REPO, session).list_branches() == ["main"]
    assert [entry[0] for entry in log] == [
        "connect",
        "attempt",
        "disconnect",
        "connect",
        "code_store_views",
    ]
