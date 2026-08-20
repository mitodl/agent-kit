"""Tests for the schema/data migration commands."""

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from .conftest import SCHEMA, _Tools, requires_omnigraph


def _init_store(path):
    subprocess.run(
        ["omnigraph", "init", "--schema", str(SCHEMA), str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return str(path)


def _insert_memory(
    client, *, slug, content, updated_at, created_at="2026-01-01T00:00:00Z"
):
    client.change(
        "mutations.gq",
        "insert_memory",
        {
            "slug": slug,
            "kind": "lesson",
            "title": "collide",
            "content": content,
            "repo": None,
            "language": None,
            "category": None,
            "severity": None,
            "author": "pytest",
            "tags": None,
            "symbol_refs": None,
            "confidence": None,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def _old_schema_store(tmp_path):
    """A store on the pre-graph-memory schema: Memory with all its required fields
    but no Topic type, no confidence, no graph edges. Applying the current schema
    over this is purely additive (the real upgrade path)."""
    schema = tmp_path / "old.pg"
    schema.write_text(
        "node Memory {\n"
        "    slug: String @key\n"
        "    kind: enum(pattern, project_fact, lesson, agent_context) @index\n"
        "    title: String @index\n"
        "    content: String @index\n"
        "    author: String @index\n"
        "    created_at: DateTime @index\n"
        "    updated_at: DateTime\n"
        "}\n"
    )
    store = tmp_path / "old.omni"
    subprocess.run(
        ["omnigraph", "init", "--schema", str(schema), str(store)],
        check=True,
        capture_output=True,
        text=True,
    )
    return str(store)


@requires_omnigraph
def test_apply_schema_is_idempotent(server):
    from witan import server as srv

    # The test store already has the current schema; re-applying succeeds and is a
    # no-op, and the Topic type is present.
    res = srv.apply_schema()
    assert res["store"]
    assert srv._topic_schema_present() is True


@requires_omnigraph
def test_apply_schema_upgrades_an_old_store(server, tmp_path, monkeypatch):
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import server as srv

    client = graph_mod.OmnigraphClient(
        _old_schema_store(tmp_path), cfg_mod.load().queries_dir
    )
    monkeypatch.setattr(srv, "client", client)

    # Pre-graph schema: probe reports the Topic type is absent...
    assert srv._topic_schema_present() is False
    # ...applying the bundled schema adds it.
    srv.apply_schema()
    assert srv._topic_schema_present() is True


def test_is_storage_version_mismatch():
    from witan import server as srv

    msg = (
        "__manifest is stamped at internal schema v3, but this omnigraph reads only v4."
    )
    assert srv._is_storage_version_mismatch(msg) is True
    assert srv._is_storage_version_mismatch("some other omnigraph error") is False


def test_migrate_storage_format_rejects_remote_stores(monkeypatch):
    from witan import server as srv

    class _FakeClient:
        graph_uri = "s3://bucket/graph.omni"

    monkeypatch.setattr(srv, "client", _FakeClient())
    try:
        srv.migrate_storage_format()
    except RuntimeError as exc:
        assert "remote store" in str(exc)
    else:
        raise AssertionError("expected RuntimeError for a remote store")


@requires_omnigraph
def test_migrate_storage_format_is_noop_when_already_readable(server):
    from witan import server as srv

    assert srv.migrate_storage_format() == {
        "migrated": False,
        "reason": "already readable by the current omnigraph binary",
    }


def _fake_binary(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


@requires_omnigraph
def test_pre_upgrade_candidates_exclude_the_current_binary(monkeypatch):
    import shutil

    from witan import server as srv

    # Isolated from the real ~/.local/bin: a developer who has upgraded once
    # legitimately HAS set-aside binaries there, and this test is about the
    # PATH scan finding nothing but the current binary.
    monkeypatch.setattr(srv.omnigraph_install, "preserved_binaries", lambda *a: [])
    current = shutil.which("omnigraph")
    assert srv._pre_upgrade_binary_candidates(current) == []


def test_pre_upgrade_candidates_offer_set_aside_binaries_first(tmp_path, monkeypatch):
    """Set-aside copies are the best guesses, so they are tried before an
    unrelated PATH install of unknown vintage — but PATH is still offered,
    because preference is not proof."""
    from witan import server as srv

    kept = [
        _fake_binary(tmp_path / "omnigraph-0.8.1"),
        _fake_binary(tmp_path / "omnigraph-0.7.2"),
    ]
    on_path = _fake_binary(tmp_path / "brew" / "omnigraph")
    monkeypatch.setattr(srv.omnigraph_install, "preserved_binaries", lambda *a: kept)
    monkeypatch.setenv("PATH", str(on_path.parent))

    assert srv._pre_upgrade_binary_candidates("/usr/local/bin/omnigraph") == [
        str(kept[0]),
        str(kept[1]),
        str(on_path),
    ]


def test_pre_upgrade_candidates_still_reach_path_when_a_stale_backup_exists(
    tmp_path, monkeypatch
):
    """`OmnigraphClient._find_binary` resolves PATH before ~/.local/bin, so a
    Homebrew binary can be the current one while an unrelated set-aside copy
    sits unused. Returning only that copy would abort the migration without
    ever scanning PATH — the regression this ordering-plus-fallthrough
    prevents."""
    from witan import server as srv

    stale = _fake_binary(tmp_path / "omnigraph-0.5.0")
    other = _fake_binary(tmp_path / "usr" / "omnigraph")
    monkeypatch.setattr(srv.omnigraph_install, "preserved_binaries", lambda *a: [stale])
    monkeypatch.setenv("PATH", str(other.parent))

    candidates = srv._pre_upgrade_binary_candidates("/opt/homebrew/bin/omnigraph")

    assert str(other) in candidates, "the PATH binary must remain reachable"


def test_pre_upgrade_candidates_drop_a_set_aside_copy_of_itself(tmp_path, monkeypatch):
    """Degenerate case: the set-aside path resolves to the binary in use. It
    cannot read a store that one refuses, so it is not a candidate."""
    from witan import server as srv

    current = _fake_binary(tmp_path / "omnigraph")
    monkeypatch.setattr(
        srv.omnigraph_install, "preserved_binaries", lambda *a: [current]
    )
    monkeypatch.setenv("PATH", "")

    assert srv._pre_upgrade_binary_candidates(str(current)) == []


@requires_omnigraph
def test_merge_adds_a_row_only_present_in_source(server, tmp_path):
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import server as srv

    source = graph_mod.OmnigraphClient(
        _init_store(tmp_path / "source.omni"), cfg_mod.load().queries_dir
    )
    _insert_memory(
        source,
        slug="mem-only-in-source-aaaaaa",
        content="hi",
        updated_at="2026-01-01T00:00:00Z",
    )

    result = srv.merge_store(source.graph_uri)

    assert (result["added"], result["updated"], result["kept_target"]) == (1, 0, 0)
    assert result["rows_loaded"] == 1
    rows = srv.client.read(
        "read.gq", "get_memory", {"slug": "mem-only-in-source-aaaaaa"}
    )
    assert rows and rows[0]["content"] == "hi"


@requires_omnigraph
def test_merge_load_is_chunked_per_table(server, tmp_path, monkeypatch):
    """`merge_store` must route its load through `chunk_records`, not send the
    whole reconciled set as one `load_batch`.

    Before omnigraph 0.9 the only ceiling on a load was the served request
    body, so a LOCAL merge could safely go in one call. 0.9 added a per-table
    row cap enforced by the engine itself, local stores included, so an
    unchunked merge of more than `LOAD_MAX_ROWS` rows of one type is now
    refused outright.

    Forces a two-row split rather than building an 8,000-row fixture: what
    regressed is the wiring, and a real-size fixture would test omnigraph's
    cap instead of this function's use of it.
    """
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import server as srv

    source = graph_mod.OmnigraphClient(
        _init_store(tmp_path / "source.omni"), cfg_mod.load().queries_dir
    )
    for i in range(3):
        _insert_memory(
            source,
            slug=f"mem-chunked-{i:02d}-cccccc",
            content=f"row {i}",
            updated_at="2026-01-01T00:00:00Z",
        )

    real_chunk = srv.chunking.chunk_records
    monkeypatch.setattr(
        srv.chunking,
        "chunk_records",
        lambda records, *a, **k: real_chunk(records, max_rows=2),
    )
    loads: list[int] = []
    real_load = graph_mod.OmnigraphClient.load_batch

    def counting_load(self, records, mode="merge"):
        loads.append(len(records))
        return real_load(self, records, mode)

    monkeypatch.setattr(graph_mod.OmnigraphClient, "load_batch", counting_load)

    result = srv.merge_store(source.graph_uri)

    assert len(loads) > 1, f"merge sent one unchunked load of {loads} rows"
    assert max(loads) <= 2
    assert sum(loads) == result["rows_loaded"] == 3
    # The rows still land despite committing across several batches.
    for i in range(3):
        rows = srv.client.read(
            "read.gq", "get_memory", {"slug": f"mem-chunked-{i:02d}-cccccc"}
        )
        assert rows and rows[0]["content"] == f"row {i}"


@requires_omnigraph
def test_merge_newer_source_row_wins_and_rerun_is_a_noop(server, tmp_path):
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import server as srv

    slug = "mem-collide-bbbbbb"
    _insert_memory(
        srv.client, slug=slug, content="old", updated_at="2026-01-01T00:00:00Z"
    )

    source = graph_mod.OmnigraphClient(
        _init_store(tmp_path / "source.omni"), cfg_mod.load().queries_dir
    )
    _insert_memory(source, slug=slug, content="new", updated_at="2026-06-01T00:00:00Z")

    result = srv.merge_store(source.graph_uri)
    assert (result["added"], result["updated"], result["kept_target"]) == (0, 1, 0)
    rows = srv.client.read("read.gq", "get_memory", {"slug": slug})
    assert rows[0]["content"] == "new"

    # Repeatable: source's row no longer strictly beats what's now in the
    # target (same timestamp, already applied), so a second run loads nothing
    # and the content is untouched.
    result2 = srv.merge_store(source.graph_uri)
    assert (result2["added"], result2["updated"], result2["kept_target"]) == (0, 0, 1)
    assert result2["rows_loaded"] == 0
    rows = srv.client.read("read.gq", "get_memory", {"slug": slug})
    assert rows[0]["content"] == "new"


@requires_omnigraph
def test_merge_older_source_row_is_dropped(server, tmp_path):
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import server as srv

    slug = "mem-collide-cccccc"
    _insert_memory(
        srv.client, slug=slug, content="current", updated_at="2026-06-01T00:00:00Z"
    )

    source = graph_mod.OmnigraphClient(
        _init_store(tmp_path / "source.omni"), cfg_mod.load().queries_dir
    )
    _insert_memory(
        source, slug=slug, content="stale", updated_at="2026-01-01T00:00:00Z"
    )

    result = srv.merge_store(source.graph_uri)
    assert (result["added"], result["updated"], result["kept_target"]) == (0, 0, 1)
    assert result["rows_loaded"] == 0
    rows = srv.client.read("read.gq", "get_memory", {"slug": slug})
    assert rows[0]["content"] == "current"


@requires_omnigraph
def test_merge_dry_run_writes_nothing(server, tmp_path):
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import server as srv

    source = graph_mod.OmnigraphClient(
        _init_store(tmp_path / "source.omni"), cfg_mod.load().queries_dir
    )
    _insert_memory(
        source,
        slug="mem-dry-run-dddddd",
        content="hi",
        updated_at="2026-01-01T00:00:00Z",
    )

    result = srv.merge_store(source.graph_uri, dry_run=True)
    assert result["dry_run"] is True
    assert result["added"] == 1
    rows = srv.client.read("read.gq", "get_memory", {"slug": "mem-dry-run-dddddd"})
    assert rows == []


@requires_omnigraph
def test_merge_strips_file_uri_prefix(server, tmp_path):
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import server as srv

    source = graph_mod.OmnigraphClient(
        _init_store(tmp_path / "source.omni"), cfg_mod.load().queries_dir
    )
    _insert_memory(
        source,
        slug="mem-file-uri-eeeeee",
        content="hi",
        updated_at="2026-01-01T00:00:00Z",
    )

    # Both source and target passed as explicit file:// URIs, as omnigraph's
    # own --store accepts — must resolve to the same plain paths OmnigraphClient
    # uses everywhere else, not a bogus relative "file:" directory.
    result = srv.merge_store(
        f"file://{source.graph_uri}", target=f"file://{srv.client.graph_uri}"
    )

    assert result["added"] == 1
    rows = srv.client.read("read.gq", "get_memory", {"slug": "mem-file-uri-eeeeee"})
    assert rows and rows[0]["content"] == "hi"
    assert not (Path.cwd() / "file:").exists()


@requires_omnigraph
def test_merge_auto_creates_missing_local_target(server, tmp_path):
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import server as srv

    source = graph_mod.OmnigraphClient(
        _init_store(tmp_path / "source.omni"), cfg_mod.load().queries_dir
    )
    _insert_memory(
        source,
        slug="mem-new-target-ffffff",
        content="hi",
        updated_at="2026-01-01T00:00:00Z",
    )

    new_target = tmp_path / "brand-new-target.omni"
    assert not new_target.exists()

    result = srv.merge_store(source.graph_uri, target=str(new_target))

    assert new_target.exists()
    assert result["added"] == 1
    target_client = graph_mod.OmnigraphClient(
        str(new_target), cfg_mod.load().queries_dir
    )
    rows = target_client.read(
        "read.gq", "get_memory", {"slug": "mem-new-target-ffffff"}
    )
    assert rows and rows[0]["content"] == "hi"


@requires_omnigraph
def test_merge_accepts_an_export_jsonl_as_source(server, tmp_path):
    """The cutover path: a store that cannot travel, handed over as its export.

    Lance embeds absolute paths, so a teammate's (or a laptop's) `.omni`
    directory can't be copied to the machine doing the merge — only
    `omnigraph export` output can. Merging straight from that file is what
    makes the local → deployed migration executable.
    """
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import server as srv

    source = graph_mod.OmnigraphClient(
        _init_store(tmp_path / "source.omni"), cfg_mod.load().queries_dir
    )
    _insert_memory(
        source,
        slug="mem-from-export-111111",
        content="handed over",
        updated_at="2026-01-01T00:00:00Z",
    )
    export = tmp_path / "handover.jsonl"
    with open(export, "w", encoding="utf-8") as f:
        subprocess.run(
            ["omnigraph", "export", "--store", source.graph_uri],
            check=True,
            stdout=f,
            text=True,
        )
    # The store the export came from is gone — only the file remains, which is
    # the whole point of exporting.
    shutil.rmtree(source.graph_uri)

    result = srv.merge_store(str(export))

    assert (result["added"], result["updated"], result["kept_target"]) == (1, 0, 0)
    rows = srv.client.read("read.gq", "get_memory", {"slug": "mem-from-export-111111"})
    assert rows and rows[0]["content"] == "handed over"


def test_merge_from_a_missing_export_file_names_the_file(server, tmp_path):
    """A `.jsonl` source is read as an export, so a typo'd path must say so
    rather than being handed to `omnigraph export` as if it were a store."""
    from witan import server as srv

    with pytest.raises(RuntimeError, match="no such export file"):
        srv.merge_store(str(tmp_path / "nope.jsonl"))


def test_merge_from_a_remote_export_says_to_download_it(server):
    """A remote `.jsonl` is a different mistake from a missing one, and
    "no such export file" would point at the wrong problem.

    An export is bytes, not a store: witan has no credentials for the bucket
    holding it and no reason to grow any, so the answer is "download it with
    whatever already has access", not "check the path".
    """
    from witan import server as srv

    for uri in (
        "s3://ol-data-witan-ci/alice.jsonl",
        "https://example.invalid/alice.jsonl",
    ):
        with pytest.raises(RuntimeError, match="does not fetch remote ones"):
            srv.merge_store(uri)


def test_merge_addresses_a_remote_target_as_server_and_graph(
    server, tmp_path, monkeypatch
):
    """A deployed graph is `--server <url> --graph <id>`, never `--store`.

    This is the regression that made the shared store unreachable from the
    merge path: omnigraph 0.8.1 rejects an http(s) `--store`, so an
    `http://…` target — which is exactly what the in-cluster maintenance pod
    has, via `WITAN_MEMORY_URI` — failed at the first export. Stubs the export
    because the assertion is about the address the client resolves, and a real
    deployed server is not available to a unit test.
    """
    from witan import server as srv

    source = tmp_path / "empty.jsonl"
    source.write_text("")
    exports = []

    def fake_export(self, path, *, label="export"):
        exports.append((self._store_args(), self._subprocess_env(), label))
        Path(path).write_text("")

    monkeypatch.setattr(srv.OmnigraphClient, "export_to", fake_export)
    srv.merge_store(
        str(source),
        target="http://omnigraph-server.omnigraph.svc:8080/graphs/council",
        dry_run=True,
    )

    # One export: the target. The source was a handed-over export file, so it
    # is read directly rather than re-exported.
    assert len(exports) == 1
    args, env, _ = exports[0]
    assert "--store" not in args
    assert args == [
        "--server",
        "http://omnigraph-server.omnigraph.svc:8080",
        "--graph",
        "council",
    ]
    # A remote store inherits the ambient bearer token — the spelling the
    # in-cluster Job's WITAN_MEMORY_TOKEN lands in.
    assert env is not None


@requires_omnigraph
def test_store_client_matches_the_configured_graph_across_spellings(monkeypatch):
    """The configured token must survive being asked for by a different spelling.

    A deployment sets `WITAN_MEMORY_URI` to a *bare* server URL plus a separate
    `WITAN_MEMORY_GRAPH`, while the runbook writes a deployed graph as
    `http://host:8080/graphs/<id>`. Those name one graph, and `WITAN_MEMORY_TOKEN`
    is the only credential the break-glass pod holds for it — comparing the raw
    strings would drop it and turn an operator's explicit `--target` into a 401.
    """
    from witan import server as srv

    class _Configured:
        graph_uri = "http://omnigraph-server.omnigraph.svc:8080"
        server_url = "http://omnigraph-server.omnigraph.svc:8080"
        graph_id = "council"
        token = "svc-witan-admin-token"
        is_remote = True

    monkeypatch.setattr(srv, "client", _Configured())
    base = _Configured.server_url

    for uri in (base, f"{base}/", f"{base}/graphs/council"):
        built = srv._store_client(uri)
        assert built._store_args() == ["--server", base, "--graph", "council"]
        assert built._subprocess_env()["OMNIGRAPH_BEARER_TOKEN"] == (
            "svc-witan-admin-token"
        )

    # Another graph on the same server, or the same graph id on a different
    # server, is *not* the configured store: it falls back to the ambient token.
    for uri in (f"{base}/graphs/code", "http://elsewhere:8080/graphs/council"):
        env = srv._store_client(uri)._subprocess_env()
        assert env.get("OMNIGRAPH_BEARER_TOKEN") != "svc-witan-admin-token"


def test_merge_into_a_bare_server_url_reports_the_missing_graph_id(server, tmp_path):
    """A remote target with no graph id is a caller error, not a crash.

    `--target http://host:8080` is the natural typo for the documented
    `http://host:8080/graphs/<id>`, and the URI parser signals it with a
    ValueError — which the CLI's `except RuntimeError` would not catch, so the
    operator got a traceback where the previous `--store` spelling gave them a
    message.
    """
    from witan import server as srv

    source = tmp_path / "empty.jsonl"
    source.write_text("")
    with pytest.raises(RuntimeError, match="has no graph id"):
        srv.merge_store(str(source), target="http://host:8080", dry_run=True)


def test_merge_refuses_an_export_file_as_the_target(server, tmp_path):
    """`.jsonl` means "export" on the source side; a target must still be a store.

    Without this the asymmetry silently eats data: a missing local target is
    auto-created, so `--target combined.jsonl` would `omnigraph init` a Lance
    store *directory* under that name and report a successful merge into a
    graph nobody will ever open.
    """
    from witan import server as srv

    source = tmp_path / "handover.jsonl"
    source.write_text("")
    target = tmp_path / "combined.jsonl"

    with pytest.raises(RuntimeError, match="must be a store"):
        srv.merge_store(str(source), target=str(target))

    assert not target.exists()


@requires_omnigraph
def test_merge_reconciles_deterministic_code_branch_slugs(server, tmp_path):
    """CodeBranch is the one slug class that genuinely collides across users.

    Memory/Task/Project/Session slugs carry a random hex suffix, so a
    cross-user collision is a ~0.015% accident. CodeBranch slugs are
    `<repo>|<branch>` by construction, so two people on the same branch of the
    same repo ALWAYS collide — this is the class the cutover has to be safe
    for. It is: the node carries only status + timestamps (the task/project
    association lives on WorksOn/ForProject edges, which have no slug and
    merge additively), so newest-wins costs at most a stale `status`, never a
    lost association.
    """
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import server as srv

    slug = "https://github.com/mitodl/agent-kit|main"

    def _branch(client, *, status, updated_at):
        client.change(
            "mutations.gq",
            "insert_code_branch",
            {
                "slug": slug,
                "repo": "https://github.com/mitodl/agent-kit",
                "branch": "main",
                "status": status,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": updated_at,
            },
        )

    # The target (shared graph) already has one user's row, marked merged.
    _branch(srv.client, status="merged", updated_at="2026-01-01T00:00:00Z")
    # A second user's local store has the same branch, still active, newer.
    source = graph_mod.OmnigraphClient(
        _init_store(tmp_path / "source.omni"), cfg_mod.load().queries_dir
    )
    _branch(source, status="active", updated_at="2026-06-01T00:00:00Z")

    result = srv.merge_store(source.graph_uri)

    decisions = {(d["type"], d["slug"]): d["decision"] for d in result["decisions"]}
    assert decisions[("CodeBranch", slug)] == "updated"
    rows = srv.client.read("read.gq", "get_code_branch", {"slug": slug})
    assert rows and rows[0]["status"] == "active"


@requires_omnigraph
def test_merge_carries_edges_across_from_a_store_that_has_them(server, tmp_path):
    """The end-to-end guard the other merge tests could not be: real edges.

    Every existing store-to-store merge test builds its fixtures from bare node
    inserts, so both exports came out edge-free and the merge never met the row
    shape that broke it. `memory_store` writes a Memory, its Topics, and the
    `Tagged` edges between them, which is enough for the source export to look
    like a real one — roughly two edge rows per node.
    """
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import server as srv

    source_store = graph_mod.OmnigraphClient(
        _init_store(tmp_path / "source.omni"), cfg_mod.load().queries_dir
    )
    original, srv.client = srv.client, source_store
    try:
        stored = _Tools(srv).memory_store(
            kind="pattern",
            title="Merging a store that has edges",
            content="An export is mostly edges; the merge has to carry them.",
            tags=["witan", "migration"],
        )
    finally:
        srv.client = original
    slug = stored["slug"]

    export = tmp_path / "source.jsonl"
    source_store.export_to(export, label="export (test source)")
    _, edges = srv._parse_export(export)
    assert edges, "fixture must produce edge rows or it does not guard anything"

    result = srv.merge_store(source_store.graph_uri)

    assert result["merged"]
    # The node came across...
    assert srv.client.read("read.gq", "get_memory", {"slug": slug})
    # ...and so did its topics, which are only reachable over a Tagged edge.
    topics = srv.client.read("read.gq", "topics_for_memory", {"slug": slug})
    assert {t["slug"] for t in topics} >= {"tp-topic-witan", "tp-topic-migration"}


def test_parse_export_raises_on_corrupted_json_line(tmp_path):
    from witan import server as srv

    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json at all\n")
    try:
        srv._parse_export(bad)
    except RuntimeError as exc:
        assert "not valid JSON" in str(exc)
    else:
        raise AssertionError("expected RuntimeError for a corrupted export line")


def test_parse_export_reads_real_edge_rows_as_edges(tmp_path):
    """An `omnigraph export` edge row has no 'type', and that is not corruption.

    This is the shape omnigraph 0.8.1 actually writes — `{"edge", "from",
    "to", "data"}` — and it outnumbers node rows roughly 2:1 in a real store.
    Treating a missing 'type' as a malformed line made every merge die on the
    first edge it met, in either direction: sourcing from a populated store, or
    targeting a deployed graph that had ever written an edge.
    """
    from witan import server as srv

    export = tmp_path / "export.jsonl"
    export.write_text(
        '{"type": "Memory", "data": {"slug": "mem-x-aaaaaa", "title": "x"}}\n'
        '{"edge": "Tagged", "from": "mem-x-aaaaaa", "to": "tp-topic-witan", '
        '"data": {"id": "01KZEH5Z994JBHHB4BHHTVH2YY"}}\n'
    )

    nodes, edges = srv._parse_export(export)

    assert set(nodes) == {("Memory", "mem-x-aaaaaa")}
    assert edges == [
        {
            "edge": "Tagged",
            "from": "mem-x-aaaaaa",
            "to": "tp-topic-witan",
            "data": {"id": "01KZEH5Z994JBHHB4BHHTVH2YY"},
        }
    ]


@pytest.mark.parametrize("payload", ["[]", "null", '"a string"', "3"])
def test_parse_export_raises_on_a_line_that_is_not_a_json_object(tmp_path, payload):
    """Valid JSON is not the same as a valid export row.

    Each of these parses cleanly and then reaches `.get` as a bare
    `AttributeError` — the raw fault this boundary exists to convert into a
    sentence naming the offending line.
    """
    from witan import server as srv

    bad = tmp_path / "bad.jsonl"
    bad.write_text(payload + "\n")
    with pytest.raises(RuntimeError, match="not a JSON object"):
        srv._parse_export(bad)


def test_parse_export_raises_on_a_row_that_is_neither_node_nor_edge(tmp_path):
    from witan import server as srv

    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"data": {"slug": "mem-x-aaaaaa"}}\n')
    try:
        srv._parse_export(bad)
    except RuntimeError as exc:
        assert "neither a node" in str(exc)
    else:
        raise AssertionError("expected RuntimeError for a row that is neither")


def test_store_merge_and_parse_export_classify_rows_identically(tmp_path):
    """The two merge transports must not disagree about what an edge is.

    They already had: `store_merge` classified inline and tolerated edges,
    `_parse_export` raised on them. Both now go through `_classify_rows`, and
    this pins that they stay one implementation — a row's fate must not depend
    on whether it arrived as a file or over the wire.
    """
    from witan import server as srv

    rows = [
        {"type": "Memory", "data": {"slug": "mem-x-aaaaaa"}},
        {"type": "Topic", "data": {"slug": "tp-topic-witan"}},
        {"edge": "Tagged", "from": "mem-x-aaaaaa", "to": "tp-topic-witan", "data": {}},
        # A node type with no slug is unreconcilable, so it passes through too.
        {"type": "Actor", "data": {"id": "act-1"}},
    ]
    export = tmp_path / "export.jsonl"
    export.write_text("".join(json.dumps(r) + "\n" for r in rows))

    assert srv._classify_rows(rows, "merge batch") == srv._parse_export(export)


def test_parse_ts_handles_missing_and_unparsable_values():
    from witan import server as srv

    assert srv._parse_ts(None) is None
    assert srv._parse_ts("") is None
    assert srv._parse_ts("not a timestamp") is None


def test_parse_ts_orders_mixed_offset_formats_correctly():
    """Raw string comparison sorts these two backwards: '...T23:30:00-05:00'
    (2026-01-02T04:30:00 UTC) is lexicographically *less* than
    '...T00:00:00Z' (2026-01-02T00:00:00 UTC, actually 4.5 hours earlier),
    because the date digit '1' < '2' dominates the comparison. Parsing to a
    real datetime is the only way to get the ordering right."""
    from witan import server as srv

    later_with_offset = srv._parse_ts("2026-01-01T23:30:00-05:00")
    earlier_utc = srv._parse_ts("2026-01-02T00:00:00Z")

    assert "2026-01-01T23:30:00-05:00" < "2026-01-02T00:00:00Z"  # wrong as strings
    assert later_with_offset > earlier_utc  # correct once parsed


# The literal integers below were captured from a real omnigraph 0.9.0
# `export` of a Memory seeded with created_at="2026-01-01T00:00:00Z" and
# updated_at="2026-08-10T12:30:45.123456Z". Hard-coded on purpose: they are the
# upstream contract, and a future release that changes the scale again should
# fail here rather than in a user's merge.
_EXPORT_MS_2026_01_01 = 1767225600000
_EXPORT_MS_2026_08_10 = 1786365045123


def test_parse_ts_reads_the_epoch_millis_that_omnigraph_0_9_exports():
    """0.8.x exported a naive ISO string; 0.9.0 exports integer epoch millis.
    `datetime.fromisoformat` raises TypeError on an int, which is what broke
    every `store_merge` on the 0.9.0 bump."""
    from witan import server as srv

    assert srv._parse_ts(_EXPORT_MS_2026_01_01) == datetime(2026, 1, 1, 0, 0, 0)
    assert srv._parse_ts(_EXPORT_MS_2026_08_10) == datetime(
        2026, 8, 10, 12, 30, 45, 123000
    )


def test_parse_ts_treats_export_integers_as_millis_not_micros():
    """The scale is the trap, not the type. `commit list --json` reports
    microseconds (witan_code.graph.branch_last_write divides by 1_000_000);
    `export` reports milliseconds. Reading one as the other does not raise —
    it silently dates every row to 1970 and inverts merge decisions."""
    from witan import server as srv

    parsed = srv._parse_ts(_EXPORT_MS_2026_01_01)

    assert parsed.year == 2026
    assert (
        datetime.fromtimestamp(_EXPORT_MS_2026_01_01 / 1_000_000, timezone.utc).year
        == 1970
    )  # what the microsecond reading would have given


def test_parse_ts_agrees_across_the_two_export_representations():
    """A merge routinely spans versions — `witan migrate merge` accepts a
    .jsonl export taken on another machine, so a 0.8.x string and a 0.9.x
    integer for the same instant must compare equal, not merely both parse."""
    from witan import server as srv

    assert srv._parse_ts("2026-01-01T00:00:00") == srv._parse_ts(_EXPORT_MS_2026_01_01)
    assert srv._parse_ts(_EXPORT_MS_2026_08_10) > srv._parse_ts("2026-01-01T00:00:00Z")


def test_parse_ts_rejects_bools_and_absurd_epochs():
    """`True` is an int subclass and would otherwise read as 1ms past the
    epoch — a value that beats nothing but compares as real."""
    from witan import server as srv

    assert srv._parse_ts(True) is None
    assert srv._parse_ts(False) is None
    assert srv._parse_ts(10**30) is None


@requires_omnigraph
def test_migrate_repo_keys_folds_task_repo_and_symbol_refs(server):
    from witan import server as srv

    stale = "https://github.com/MITODL/OL-Django"
    canonical = "https://github.com/mitodl/ol-django"
    srv.client.change(
        "mutations.gq",
        "insert_task",
        {
            "slug": "tk-stale-case-aaaaaa",
            "title": "stale",
            "description": "",
            "repo": stale,
            "type": "task",
            "status": "open",
            "priority": "p2",
            "project_slug": None,
            "parent_slug": None,
            "blocked_by": None,
            "assignee": None,
            "external_uri": None,
            "author": "pytest",
            "symbol_refs": [f"{stale}#app.py::Foo"],
            "tags": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "claimed_at": None,
        },
    )

    result = srv.migrate_repo_keys()
    assert result["tasks_updated"] == 1
    assert result["repos_changed"] == {stale: canonical}

    row = srv.client.read("read.gq", "get_task", {"slug": "tk-stale-case-aaaaaa"})[0]
    assert row["repo"] == canonical
    assert row["symbol_refs"] == [f"{canonical}#app.py::Foo"]

    # Idempotent: nothing left to fold on a second run.
    assert srv.migrate_repo_keys()["tasks_updated"] == 0


@requires_omnigraph
def test_migrate_repo_keys_folds_memory_repo(server):
    from witan import server as srv

    stale = "https://GitHub.com/mitodl/ol-django"
    canonical = "https://github.com/mitodl/ol-django"
    _insert_memory(
        srv.client,
        slug="mem-stale-case-bbbbbb",
        content="hi",
        updated_at="2026-01-01T00:00:00Z",
    )
    # _insert_memory hardcodes repo=None; overwrite it directly via update_memory
    # so this test controls the exact stale value under migration.
    srv.client.change(
        "mutations.gq",
        "update_memory",
        {
            "slug": "mem-stale-case-bbbbbb",
            "title": "collide",
            "content": "hi",
            "repo": stale,
            "language": None,
            "category": None,
            "severity": None,
            "tags": None,
            "symbol_refs": None,
            "confidence": None,
            "updated_at": "2026-01-01T00:00:00Z",
        },
    )

    result = srv.migrate_repo_keys()
    assert result["memories_updated"] == 1
    row = srv.client.read("read.gq", "get_memory", {"slug": "mem-stale-case-bbbbbb"})[0]
    assert row["repo"] == canonical


@requires_omnigraph
def test_migrate_repo_keys_folds_and_dedupes_project_repos(server):
    from witan import server as srv

    srv.client.change(
        "mutations.gq",
        "insert_workflow_project",
        {
            "slug": "wp-stale-case-cccccc",
            "title": "stale project",
            "description": "",
            "repos": [
                "https://github.com/MITODL/OL-Django",
                "https://github.com/mitodl/ol-django",
            ],
            "status": "active",
            "phase": "discovery",
            "author": "pytest",
            "tags": None,
            "github_issue": None,
            "github_pr": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
    )

    result = srv.migrate_repo_keys()
    assert result["projects_updated"] == 1

    row = srv.client.read(
        "read.gq", "get_workflow_project", {"slug": "wp-stale-case-cccccc"}
    )[0]
    # Both entries fold onto the same canonical key and dedupe to one.
    assert row["repos"] == ["https://github.com/mitodl/ol-django"]


@requires_omnigraph
def test_migrate_repo_keys_recreates_code_branch_under_canonical_slug(server):
    from witan import server as srv

    stale = "https://github.com/MITODL/OL-Django"
    canonical = "https://github.com/mitodl/ol-django"
    stale_slug = f"{stale}|feature/x"
    canonical_slug = f"{canonical}|feature/x"
    srv.client.change(
        "mutations.gq",
        "insert_code_branch",
        {
            "slug": stale_slug,
            "repo": stale,
            "branch": "feature/x",
            "status": "active",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
    )
    # WorksOn/ForProject edges on the stale branch must survive the move to
    # the canonical slug, else "In-Flight Branch" context silently drops them.
    srv.client.change(
        "mutations.gq",
        "insert_task",
        {
            "slug": "tk-branch-carries-dddddd",
            "title": "carried",
            "description": "",
            "repo": None,
            "type": "task",
            "status": "open",
            "priority": "p2",
            "project_slug": None,
            "parent_slug": None,
            "blocked_by": None,
            "assignee": None,
            "external_uri": None,
            "author": "pytest",
            "symbol_refs": None,
            "tags": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "claimed_at": None,
        },
    )
    srv.client.change(
        "mutations.gq",
        "insert_workflow_project",
        {
            "slug": "wp-branch-carries-eeeeee",
            "title": "carried project",
            "description": "",
            "repos": None,
            "status": "active",
            "phase": "discovery",
            "author": "pytest",
            "tags": None,
            "github_issue": None,
            "github_pr": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
    )
    srv.client.change(
        "mutations.gq",
        "link_works_on",
        {"from": stale_slug, "to": "tk-branch-carries-dddddd"},
    )
    srv.client.change(
        "mutations.gq",
        "link_for_project",
        {"from": stale_slug, "to": "wp-branch-carries-eeeeee"},
    )

    result = srv.migrate_repo_keys()
    assert result["code_branches_migrated"] == 1

    new_row = srv.client.read("read.gq", "get_code_branch", {"slug": canonical_slug})
    assert (
        new_row and new_row[0]["repo"] == canonical and new_row[0]["status"] == "active"
    )

    old_row = srv.client.read("read.gq", "get_code_branch", {"slug": stale_slug})
    assert old_row and old_row[0]["status"] == "abandoned"

    carried_tasks = srv.client.read(
        "read.gq", "code_branch_tasks", {"branch_slug": canonical_slug}
    )
    assert [t["slug"] for t in carried_tasks] == ["tk-branch-carries-dddddd"]
    carried_projects = srv.client.read(
        "read.gq", "code_branch_projects", {"branch_slug": canonical_slug}
    )
    assert [p["slug"] for p in carried_projects] == ["wp-branch-carries-eeeeee"]

    # Idempotent: the stale row is now abandoned, so re-running skips it.
    assert srv.migrate_repo_keys()["code_branches_migrated"] == 0


@requires_omnigraph
def test_migrate_repo_keys_merges_edges_when_canonical_branch_preexists(server):
    """A session can create the canonical-slug CodeBranch (e.g. via
    task_claim) after the case-fold fix ships but before this migration runs.
    The migration must still merge the stale row's WorksOn/ForProject edges
    onto that pre-existing canonical branch, not skip it outright — else the
    association silently disappears once reads move to the canonical slug."""
    from witan import server as srv

    stale = "https://github.com/MITODL/OL-Django"
    canonical = "https://github.com/mitodl/ol-django"
    stale_slug = f"{stale}|feature/y"
    canonical_slug = f"{canonical}|feature/y"

    srv.client.change(
        "mutations.gq",
        "insert_code_branch",
        {
            "slug": stale_slug,
            "repo": stale,
            "branch": "feature/y",
            "status": "active",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
    )
    # The canonical branch already exists — created independently, e.g. by a
    # session running after the fix but before the migration.
    srv.client.change(
        "mutations.gq",
        "insert_code_branch",
        {
            "slug": canonical_slug,
            "repo": canonical,
            "branch": "feature/y",
            "status": "active",
            "created_at": "2026-02-01T00:00:00Z",
            "updated_at": "2026-02-01T00:00:00Z",
        },
    )
    srv.client.change(
        "mutations.gq",
        "insert_task",
        {
            "slug": "tk-preexist-branch-ffffff",
            "title": "carried onto preexisting branch",
            "description": "",
            "repo": None,
            "type": "task",
            "status": "open",
            "priority": "p2",
            "project_slug": None,
            "parent_slug": None,
            "blocked_by": None,
            "assignee": None,
            "external_uri": None,
            "author": "pytest",
            "symbol_refs": None,
            "tags": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "claimed_at": None,
        },
    )
    srv.client.change(
        "mutations.gq",
        "link_works_on",
        {"from": stale_slug, "to": "tk-preexist-branch-ffffff"},
    )

    result = srv.migrate_repo_keys()
    assert result["code_branches_migrated"] == 1

    old_row = srv.client.read("read.gq", "get_code_branch", {"slug": stale_slug})
    assert old_row and old_row[0]["status"] == "abandoned"

    carried_tasks = srv.client.read(
        "read.gq", "code_branch_tasks", {"branch_slug": canonical_slug}
    )
    assert [t["slug"] for t in carried_tasks] == ["tk-preexist-branch-ffffff"]

    # No duplicate WorksOn edge from re-running against an unchanged store.
    result2 = srv.migrate_repo_keys()
    assert result2["code_branches_migrated"] == 0
    carried_tasks_again = srv.client.read(
        "read.gq", "code_branch_tasks", {"branch_slug": canonical_slug}
    )
    assert [t["slug"] for t in carried_tasks_again] == ["tk-preexist-branch-ffffff"]


@requires_omnigraph
def test_migrate_repo_keys_is_noop_on_already_canonical_store(server):
    from witan import server as srv

    result = srv.migrate_repo_keys()
    assert result == {
        "tasks_updated": 0,
        "memories_updated": 0,
        "sessions_updated": 0,
        "projects_updated": 0,
        "traces_updated": 0,
        "code_branches_migrated": 0,
        "repos_changed": {},
    }


@requires_omnigraph
def test_store_merge_reports_an_unreachable_data_tier_as_a_retryable_sentence(
    server, monkeypatch
):
    """The user hitting this cannot see the data tier, so tell them what to do.

    `store_merge` is the server-side half of the self-service cutover: the
    caller is a person migrating their own graph through the MCP tier. When the
    data tier stays down for the whole retry budget, relaying omnigraph's error
    hands them a Rust backtrace naming a ClusterIP they have no access to. What
    they can act on is that it is transient and that re-running is safe.
    """
    from witan import server as srv

    def refuse(self, path, *, label="export"):
        raise srv.StoreUnavailable(
            f"omnigraph {label} failed after 9 attempts over 150s — could not "
            "connect to http://omnigraph-server.omnigraph.svc.cluster.local:8080"
        )

    monkeypatch.setattr(srv.OmnigraphClient, "export_to", refuse)

    with pytest.raises(RuntimeError) as excinfo:
        server.store_merge([{"type": "Memory", "data": {"slug": "mem-x"}}])

    message = str(excinfo.value)
    assert "temporarily unavailable" in message
    assert "re-run" in message.lower()
    # The internal address and the backtrace stay off the user's screen, but
    # remain on __cause__ for the operator reading logs.
    assert "cluster.local" not in message
    assert isinstance(excinfo.value.__cause__, srv.StoreUnavailable)


@requires_omnigraph
def test_store_merge_covers_the_load_leg_not_only_the_export(server, monkeypatch):
    """The outage can just as easily land between the export and the load.

    Wrapping only the export would leave the second half of the same tool call
    relaying the raw error — same user, same restart, same advice needed.
    """
    from witan import server as srv

    def refuse(self, records, mode="merge"):
        raise srv.StoreUnavailable(
            "omnigraph load failed after 9 attempts over 150s — could not "
            "connect to http://omnigraph-server.omnigraph.svc.cluster.local:8080"
        )

    monkeypatch.setattr(srv.OmnigraphClient, "load_batch", refuse)

    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        server.store_merge([{"type": "Memory", "data": {"slug": "mem-y"}}])


@requires_omnigraph
def test_store_merge_rows_are_findable_by_search_not_just_readable(server):
    """A merged row must come back from ``memory_search``, not merely ``memory_get``.

    Rows landing and rows being *findable* are different claims, and until this
    test only the first was ever asserted. That distinction is what
    tk-bm25-search-returns-nothing-on-the-ci-council-gr-ec3cfa turned on: the
    memory it reported was listable by slug and by kind on the deployed graph
    while every search for it came back empty.

    Note what this can and cannot catch. It pins the *witan-side* path — that
    ``store_merge`` writes content into the field ``read.gq``'s search queries
    actually match on, and that nothing in the merge drops or mangles it. It
    cannot reproduce the deployed symptom, which is a corpus-size property of
    the engine's BM25: a term whose document frequency approaches the corpus
    size scores non-positive and is dropped, and one such term zeroes the whole
    query. See docs/store-merge-findings.md, "Search looks broken on a
    near-empty graph".

    Hence the filler rows below. Rarity is ``df/N``, not a property of the word
    itself: in a one-row corpus the search token would appear in *every*
    document, which is precisely the regime this test must stay out of if it is
    to isolate ``store_merge``. The filler shares no vocabulary with the token,
    so ``df == 1`` against a corpus of five and the search stays in the
    well-behaved part of BM25 on any engine.
    """
    from witan import server as srv

    # Corpus, so the searched-for token is rare *relative to N*. Written via
    # memory_store rather than the merge, to keep the path under test to the
    # one row whose findability is being asserted.
    #
    # Through the `server` fixture, NOT the `srv` module: memory_store is a
    # coroutine function, and calling it off the module returns a coroutine
    # nobody awaits — the rows silently never land, leaving this test on the
    # one-row corpus the docstring says it must stay out of. The fixture's
    # _Tools proxy is what runs it to completion.
    for i, subject in enumerate(
        ["sourdough starters", "derailleur cables", "ski wax", "espresso pucks"]
    ):
        server.memory_store(
            kind="project_fact",
            title=f"filler {i}",
            content=f"Unrelated prose concerning {subject} and nothing else.",
        )

    now = "2026-08-07T00:00:00Z"
    rows = [
        {
            "type": "Memory",
            "data": {
                "slug": "pat-merged-findable-aaaaaa",
                "kind": "pattern",
                "title": "merged row",
                "content": "quokkazebra is a deliberately rare token",
                "repo": "https://github.com/test/repo",
                "author": "pytest",
                "created_at": now,
                "updated_at": now,
            },
        }
    ]

    assert srv.store_merge(rows)["rows_loaded"] == 1

    # Readable by slug — the weaker claim, true even when search is broken.
    assert srv.memory_get("pat-merged-findable-aaaaaa")["title"] == "merged row"

    # Findable by search — the claim that actually matters for recall.
    hits = srv.memory_search("quokkazebra")
    assert [h["slug"] for h in hits] == ["pat-merged-findable-aaaaaa"]


def test_parse_ts_compares_naive_and_aware_without_raising():
    """omnigraph's own export strips the offset witan writes down to a naive
    string — an aware value must still compare against a naive one without
    datetime's usual TypeError."""
    from witan import server as srv

    naive = srv._parse_ts("2026-01-01T12:00:00")
    aware_same_instant = srv._parse_ts("2026-01-01T12:00:00+00:00")

    assert naive == aware_same_instant


# ── `witan migrate merge --from/--to` ─────────────────────────────────────────
#
# The two named-target flags: `--from <name>`/`--to <name>` resolve a
# `[targets.<name>]` block, where `--target` stays a literal store URI. Every
# test here writes its own config.toml — the autouse `no_real_remote` fixture
# points WITAN_CONFIG at a path that does not exist, and a later `setenv` wins.


@pytest.fixture
def targets_config(monkeypatch, tmp_path):
    """Write a config.toml holding the given `[targets.*]` blocks, and select it."""

    def _write(text):
        path = tmp_path / "config.toml"
        path.write_text(text)
        monkeypatch.setenv("WITAN_CONFIG", str(path))
        return path

    return _write


def test_merge_from_resolves_a_named_targets_server(targets_config, monkeypatch):
    from witan.cli import migrate as cli_migrate

    targets_config('[targets.personal]\nserver = "~/stores/personal.omni"\n')
    monkeypatch.setenv("HOME", "/home/someone")

    assert (
        cli_migrate._merge_source(None, "personal")
        == "/home/someone/stores/personal.omni"
    )


def test_merge_from_a_remote_only_target_says_it_has_no_local_store(
    targets_config, capsys
):
    """`--from production` cannot work — there is no remote-export tool — so
    the only honest outcome is a loud failure rather than a silent no-op."""
    from witan.cli import migrate as cli_migrate

    targets_config(
        "[targets.production]\n"
        'remote_url = "https://witan.example.org/mcp"\n'
        'oidc_issuer = "https://sso.example.org/realms/eng"\n'
    )

    with pytest.raises(SystemExit):
        cli_migrate._merge_source(None, "production")
    assert "no local store" in capsys.readouterr().out.lower()


def test_merge_from_an_undefined_target_lists_the_defined_ones(targets_config, capsys):
    from witan.cli import migrate as cli_migrate

    targets_config('[targets.work]\nserver = "/tmp/work.omni"\n')

    with pytest.raises(SystemExit):
        cli_migrate._merge_source(None, "typo")
    assert "work" in capsys.readouterr().out


def test_merge_without_a_source_or_from_says_so(targets_config):
    from witan.cli import migrate as cli_migrate

    targets_config("")

    with pytest.raises(SystemExit):
        cli_migrate._merge_source(None, None)


def test_merge_rejects_a_source_and_from_together(targets_config):
    from witan.cli import migrate as cli_migrate

    targets_config('[targets.work]\nserver = "/tmp/work.omni"\n')

    with pytest.raises(SystemExit):
        cli_migrate._merge_source("/tmp/other.omni", "work")


def test_merge_rejects_to_and_target_together(targets_config, capsys):
    """Both name the destination. Picking one silently would make which store
    gets written depend on a precedence rule nobody stated."""
    from witan.cli import migrate as cli_migrate

    targets_config('[targets.work]\nserver = "/tmp/work.omni"\n')

    with pytest.raises(SystemExit):
        cli_migrate._merge_destination("work", "/tmp/elsewhere.omni")
    assert "--target" in capsys.readouterr().out


def test_merge_to_a_local_target_dispatches_in_process(targets_config, monkeypatch):
    from witan import server as srv
    from witan.cli import migrate as cli_migrate

    targets_config('[targets.work]\nserver = "~/stores/work.omni"\n')
    monkeypatch.setenv("HOME", "/home/someone")

    provider, target = cli_migrate._merge_destination("work", None)

    assert provider is srv
    assert target == "/home/someone/stores/work.omni"


def test_merge_to_a_remote_target_builds_that_deployments_proxy(
    targets_config, monkeypatch
):
    """`--to <name>` is the explicit spelling of `WITAN_TARGET=<name>`: it
    builds the named deployment's proxy directly, so the destination does not
    depend on what the environment happens to point at. The proxy refuses a
    client-named `target`, so none is passed with it."""
    from witan.cli import migrate as cli_migrate

    targets_config(
        "[targets.production]\n"
        'remote_url = "https://witan.example.org/mcp"\n'
        'oidc_issuer = "https://sso.example.org/realms/eng"\n'
        "\n"
        "[targets.other]\n"
        'remote_url = "https://elsewhere.example.org/mcp"\n'
        'oidc_issuer = "https://sso.example.org/realms/eng"\n'
    )
    monkeypatch.setenv("WITAN_TARGET", "other")
    built = []
    monkeypatch.setattr(
        cli_migrate, "remote_proxy", lambda remote: built.append(remote) or "proxy"
    )

    provider, target = cli_migrate._merge_destination("production", None)

    assert (provider, target) == ("proxy", None)
    assert [r.url for r in built] == ["https://witan.example.org/mcp"]


def test_merge_to_a_target_configuring_neither_end_is_refused(targets_config):
    from witan.cli import migrate as cli_migrate

    targets_config('[targets.stub]\nmatch_orgs = ["mitodl"]\n')

    with pytest.raises(SystemExit):
        cli_migrate._merge_destination("stub", None)


def test_merge_without_the_new_flags_is_unchanged(targets_config, monkeypatch):
    """Neither flag given resolves exactly as before: the positional source and
    the ambient `_srv()` destination."""
    from witan.cli import migrate as cli_migrate

    targets_config("")
    sentinel = object()
    monkeypatch.setattr(cli_migrate, "_srv", lambda: sentinel)

    assert cli_migrate._merge_source("/tmp/a.omni", None) == "/tmp/a.omni"
    assert cli_migrate._merge_destination(None, "/tmp/b.omni") == (
        sentinel,
        "/tmp/b.omni",
    )


def test_merge_from_a_remote_target_folds_in_its_graph_id(targets_config):
    """A target keeps its graph id in a sibling `graph` key, and the merge
    addresses a store it holds no client for — so a bare server URL reaches
    `store_cli_args` with no id and is rejected. Fold it into the URI."""
    from witan.cli import migrate as cli_migrate

    targets_config(
        '[targets.work]\nserver = "http://witan.internal:8080"\ngraph = "council"\n'
    )

    assert (
        cli_migrate._merge_source(None, "work")
        == "http://witan.internal:8080/graphs/council"
    )


def test_merge_from_a_remote_target_keeps_an_explicit_graph_path(targets_config):
    from witan.cli import migrate as cli_migrate

    targets_config(
        '[targets.work]\nserver = "http://witan.internal:8080/graphs/other"\n'
        'graph = "council"\n'
    )

    assert (
        cli_migrate._merge_source(None, "work")
        == "http://witan.internal:8080/graphs/other"
    )


def test_merge_from_a_target_with_a_per_store_token_is_refused(targets_config, capsys):
    """Nothing on the merge path carries a per-store credential — it takes the
    configured client's token or an ambient one. Authenticating to that server
    with whatever happens to be exported is worse than refusing."""
    from witan.cli import migrate as cli_migrate

    targets_config(
        "[targets.work]\n"
        'server = "http://witan.internal:8080"\n'
        'graph = "council"\n'
        'token = "sekrit"\n'
    )

    with pytest.raises(SystemExit):
        cli_migrate._merge_source(None, "work")
    out = capsys.readouterr().out
    assert "OMNIGRAPH_BEARER_TOKEN" in out
    assert "sekrit" not in out


def test_merge_from_a_file_uri_target_keeps_the_double_slash(targets_config):
    """`Path("file:///tmp/a.omni")` collapses to `file:/tmp/a.omni`, which then
    slips past `merge_store`'s `file://` strip and is read as a relative path
    named `file:`. Strip the scheme here instead."""
    from witan.cli import migrate as cli_migrate

    targets_config('[targets.archive]\nserver = "file:///tmp/a.omni"\n')

    assert cli_migrate._merge_source(None, "archive") == "/tmp/a.omni"


def test_merge_to_an_s3_target_is_left_alone(targets_config):
    from witan.cli import migrate as cli_migrate

    targets_config('[targets.cold]\nserver = "s3://bucket/graph.omni"\n')

    _, target = cli_migrate._merge_destination("cold", None)
    assert target == "s3://bucket/graph.omni"


@requires_omnigraph
def test_merge_from_and_to_reconciles_two_named_local_stores(
    server, tmp_path, targets_config, capsys
):
    """The reconciliation coverage above, driven entirely by target names —
    neither end of the merge is a path anybody typed."""
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan.cli.migrate import merge

    source_uri = _init_store(tmp_path / "personal.omni")
    target_uri = _init_store(tmp_path / "work.omni")
    queries = cfg_mod.load().queries_dir
    source = graph_mod.OmnigraphClient(source_uri, queries)
    target = graph_mod.OmnigraphClient(target_uri, queries)

    _insert_memory(
        source,
        slug="mem-only-in-source-aaaaaa",
        content="new",
        updated_at="2026-06-01T00:00:00Z",
    )
    _insert_memory(
        source,
        slug="mem-collides-bbbbbb",
        content="newer",
        updated_at="2026-06-01T00:00:00Z",
    )
    _insert_memory(
        target,
        slug="mem-collides-bbbbbb",
        content="older",
        updated_at="2026-01-01T00:00:00Z",
    )

    targets_config(
        f'[targets.personal]\nserver = "{source_uri}"\n\n'
        f'[targets.work]\nserver = "{target_uri}"\n'
    )

    merge(from_="personal", to="work")

    # Unwrapped: rich wraps the summary line to the terminal width, which is
    # narrower under CI than locally, so a raw substring match is a width test.
    out = " ".join(capsys.readouterr().out.split())
    assert "1 added, 1 updated" in out
    rows = target.read("read.gq", "get_memory", {"slug": "mem-collides-bbbbbb"})
    assert rows[0]["content"] == "newer"
    assert target.read("read.gq", "get_memory", {"slug": "mem-only-in-source-aaaaaa"})

    # Repeatable: a second run finds every row already applied.
    merge(from_="personal", to="work")
    assert "0 added" in " ".join(capsys.readouterr().out.split())
