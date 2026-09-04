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
    _, edges, _ = srv._parse_export(export)
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

    nodes, edges, _ = srv._parse_export(export)

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


# ── Authorship on migration (#267) ────────────────────────────────────────


def _export_rows(client) -> list[dict]:
    """A store's rows in `omnigraph export` shape, for `store_merge`."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "export.jsonl"
        client.export_to(out, label="test export")
        return [json.loads(line) for line in out.read_text().splitlines() if line]


@requires_omnigraph
def test_store_merge_claims_rows_authored_by_the_caller(server, tmp_path, monkeypatch):
    """The reported case: a store merged under a local identity stays yours.

    Local stdio writes `cfg.author` (git user.name); a deployment resolves
    `preferred_username`. The two never converge, so without this the row keeps
    a name the deployed identity cannot match and `memory_delete` refuses its
    own author forever.
    """
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import server as srv

    source = graph_mod.OmnigraphClient(
        _init_store(tmp_path / "source.omni"), cfg_mod.load().queries_dir
    )
    _insert_memory(
        source,
        slug="mem-migrated-by-me-aaaaaa",
        content="mine",
        updated_at="2026-01-01T00:00:00Z",
    )
    monkeypatch.setattr(srv, "_current_author", lambda: "me@example.org")

    result = srv.store_merge(_export_rows(source), claim_from_author="pytest")

    assert result["authorship_claimed"] == 1
    rows = srv.client.read(
        "read.gq", "get_memory", {"slug": "mem-migrated-by-me-aaaaaa"}
    )
    assert rows[0]["author"] == "me@example.org"


@requires_omnigraph
def test_store_merge_leaves_someone_elses_rows_alone(server, tmp_path, monkeypatch):
    """Merging a teammate's export through your credential must not reattribute
    their work — the runbook supports that merge, and the original name is kept
    nowhere else, so a silent rewrite would be unrecoverable."""
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import server as srv

    source = graph_mod.OmnigraphClient(
        _init_store(tmp_path / "source.omni"), cfg_mod.load().queries_dir
    )
    _insert_memory(
        source,
        slug="mem-written-by-them-bbbbb",
        content="theirs",
        updated_at="2026-01-01T00:00:00Z",
    )
    monkeypatch.setattr(srv, "_current_author", lambda: "me@example.org")

    # The caller's local identity is "someone-else"; the row says "pytest".
    result = srv.store_merge(_export_rows(source), claim_from_author="someone-else")

    assert result["authorship_claimed"] == 0
    rows = srv.client.read(
        "read.gq", "get_memory", {"slug": "mem-written-by-them-bbbbb"}
    )
    assert rows[0]["author"] == "pytest"


@requires_omnigraph
def test_store_merge_without_the_argument_is_unchanged(server, tmp_path, monkeypatch):
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import server as srv

    source = graph_mod.OmnigraphClient(
        _init_store(tmp_path / "source.omni"), cfg_mod.load().queries_dir
    )
    _insert_memory(
        source,
        slug="mem-no-claim-ccccccc",
        content="x",
        updated_at="2026-01-01T00:00:00Z",
    )
    monkeypatch.setattr(srv, "_current_author", lambda: "me@example.org")

    result = srv.store_merge(_export_rows(source))

    assert result["authorship_claimed"] == 0
    rows = srv.client.read("read.gq", "get_memory", {"slug": "mem-no-claim-ccccccc"})
    assert rows[0]["author"] == "pytest"


@requires_omnigraph
def test_claim_authorship_repairs_an_already_migrated_row(server, monkeypatch):
    """The whole point of the repair command: a re-merge cannot fix these.

    Reconciliation is newest-record-wins, so a re-sent row loses to its own
    already-applied copy — which is why `store_merge`'s claim does nothing for
    a store merged before it existed.
    """
    from witan import server as srv

    m = server.memory_store(kind="lesson", title="already here", content="x")
    srv.client.change(
        "mutations.gq",
        "set_memory_author",
        {"slug": m["slug"], "author": "Old Local Name", "updated_at": srv.now_iso()},
    )
    monkeypatch.setattr(srv, "_current_author", lambda: "me@example.org")

    dry = srv.claim_authorship(was="Old Local Name")
    assert dry["applied"] is False
    assert dry["claimed"] == 1
    assert dry["by_type"] == {"Memory": 1}
    rows = srv.client.read("read.gq", "get_memory", {"slug": m["slug"]})
    assert rows[0]["author"] == "Old Local Name", "dry run must write nothing"

    applied = srv.claim_authorship(was="Old Local Name", apply=True)
    assert applied["claimed"] == 1
    rows = srv.client.read("read.gq", "get_memory", {"slug": m["slug"]})
    assert rows[0]["author"] == "me@example.org"

    # Idempotent: the rows now carry the new identity.
    assert srv.claim_authorship(was="Old Local Name", apply=True)["claimed"] == 0


@requires_omnigraph
def test_claim_authorship_makes_a_migrated_memory_deletable(server, monkeypatch):
    """End to end on the reported symptom: `memory_delete` no-ops before the
    repair and succeeds after it."""
    from witan import server as srv

    m = server.memory_store(kind="lesson", title="prune me", content="x")
    srv.client.change(
        "mutations.gq",
        "set_memory_author",
        {"slug": m["slug"], "author": "Old Local Name", "updated_at": srv.now_iso()},
    )
    monkeypatch.setattr(srv, "_current_author", lambda: "me@example.org")

    refused = server.memory_delete(m["slug"], confirm=True)
    assert refused["deleted"] is False
    assert "only the author can delete" in refused["reason"]

    srv.claim_authorship(was="Old Local Name", apply=True)

    assert server.memory_delete(m["slug"], confirm=True)["deleted"] is True


@requires_omnigraph
def test_claim_authorship_refuses_to_run_against_your_own_identity(server, monkeypatch):
    from witan import server as srv

    monkeypatch.setattr(srv, "_current_author", lambda: "me@example.org")
    result = srv.claim_authorship(was="me@example.org", apply=True)

    assert result["claimed"] == 0
    assert "already carry this identity" in result["reason"]


@requires_omnigraph
def test_claim_authorship_covers_every_authored_node_type(server):
    """A type left out of `_AUTHORSHIP_SOURCES` is a row nobody can ever repair,
    and nothing else would fail — so pin the list against the schema."""
    from witan import server as srv

    schema = (Path(srv.__file__).parent.parent / "schema" / "schema.pg").read_text()
    declared = set()
    current = None
    for line in schema.splitlines():
        stripped = line.strip()
        if stripped.startswith("node "):
            current = stripped.split()[1].rstrip("{").strip()
        # Edges carry an `author:` too (schema.pg § Memory edge properties) and
        # it is NOT repairable authorship — edges have no key, so there is no
        # row for `claim_authorship` to address. Clearing `current` is what
        # stops the block being read as a continuation of the node above it.
        elif stripped.startswith("edge "):
            current = None
        elif current and stripped.startswith("author:"):
            declared.add(current)

    assert declared == set(srv._AUTHORED_TYPES)
    assert {t for t, *_ in srv._AUTHORSHIP_SOURCES} == declared


@requires_omnigraph
def test_claim_authorship_cli_defaults_was_to_the_local_author(server, monkeypatch):
    """The CLI's one piece of logic beyond dispatch: `--was` defaults to
    `config.load().author`, which is the whole point of the ergonomics — the
    person repairing their own cutover should not have to remember what their
    git `user.name` was on the day they merged."""
    from witan.cli import migrate as cli_migrate

    monkeypatch.setattr(cli_migrate, "_srv", lambda: _Recorder(), raising=False)

    from witan import config as cfg_mod

    monkeypatch.setattr(
        cfg_mod, "load", lambda *a, **k: _StubCfg(author="Old Local Name")
    )

    cli_migrate.claim_authorship()

    assert _Recorder.last == {"was": "Old Local Name", "apply": False}


class _StubCfg:
    def __init__(self, author):
        self.author = author


class _Recorder:
    last: dict | None = None

    def claim_authorship(self, *, was, apply):
        _Recorder.last = {"was": was, "apply": apply}
        return {
            "applied": apply,
            "was": was,
            "now": "me@example.org",
            "claimed": 0,
            "by_type": {},
        }


@requires_omnigraph
def test_authorship_claimed_counts_only_rows_actually_written(
    server, tmp_path, monkeypatch
):
    """`authorship_claimed` must describe effects, not intentions.

    The restamp happens before reconciliation, so a matching source row that
    LOSES to a newer target copy is discarded carrying its new author. Counting
    the rewrite would report a claim against a batch that left every stored
    author exactly as it found it.
    """
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import server as srv

    # Target holds the newer copy, so the source row loses reconciliation.
    _insert_memory(
        srv.client,
        slug="mem-loses-reconcile-ddddd",
        content="target wins",
        updated_at="2026-06-01T00:00:00Z",
    )
    source = graph_mod.OmnigraphClient(
        _init_store(tmp_path / "source.omni"), cfg_mod.load().queries_dir
    )
    _insert_memory(
        source,
        slug="mem-loses-reconcile-ddddd",
        content="source is older",
        updated_at="2026-01-01T00:00:00Z",
    )
    monkeypatch.setattr(srv, "_current_author", lambda: "me@example.org")

    result = srv.store_merge(_export_rows(source), claim_from_author="pytest")

    assert result["kept_target"] == 1
    assert result["authorship_claimed"] == 0, (
        "the losing row was discarded; nothing was reattributed"
    )
    rows = srv.client.read(
        "read.gq", "get_memory", {"slug": "mem-loses-reconcile-ddddd"}
    )
    assert rows[0]["author"] == "pytest"


@requires_omnigraph
def test_claim_authorship_bumps_updated_at_where_the_type_has_one(server, monkeypatch):
    """`WorkflowProject` reconciles on `updated_at` (`_RECONCILE_TS_FIELDS`), so
    a repair that left it stale would make the row lose a later merge against
    its own pre-repair copy."""
    from witan import server as srv

    proj = server.workflow_project_create(title="stale ts", description="d")
    srv.client.change(
        "mutations.gq",
        "set_workflow_project_author",
        {
            "slug": proj["slug"],
            "author": "Old Local Name",
            "updated_at": "2026-01-01T00:00:00Z",
        },
    )
    monkeypatch.setattr(srv, "_current_author", lambda: "me@example.org")

    srv.claim_authorship(was="Old Local Name", apply=True)

    row = srv.client.read("read.gq", "get_workflow_project", {"slug": proj["slug"]})[0]
    assert row["author"] == "me@example.org"
    assert srv._parse_ts(row["updated_at"]) > srv._parse_ts("2026-01-01T00:00:00Z")


@requires_omnigraph
def test_claim_authorship_batches_its_writes(server, monkeypatch):
    """A whole-store repair writing per row leaves one Lance version per slug —
    the fragmentation `_backfill_topics` batches to avoid, against a data tier
    where a deployed commit runs seconds."""
    from witan import server as srv

    slugs = []
    for i in range(3):
        m = server.memory_store(kind="lesson", title=f"row {i}", content="x")
        slugs.append(m["slug"])
        srv.client.change(
            "mutations.gq",
            "set_memory_author",
            {
                "slug": m["slug"],
                "author": "Old Local Name",
                "updated_at": srv.now_iso(),
            },
        )
    monkeypatch.setattr(srv, "_current_author", lambda: "me@example.org")

    batched: list[int] = []
    real_change_many = srv.client.change_many
    per_row = []
    real_change = srv.client.change

    def spy_many(steps, **kw):
        batched.append(len(steps))
        return real_change_many(steps, **kw)

    def spy_change(*a, **kw):
        per_row.append(a[1] if len(a) > 1 else None)
        return real_change(*a, **kw)

    monkeypatch.setattr(srv.client, "change_many", spy_many)
    monkeypatch.setattr(srv.client, "change", spy_change)

    srv.claim_authorship(was="Old Local Name", apply=True)

    assert batched == [3], f"expected one batched flush, got {batched}"
    assert not [q for q in per_row if q and q.startswith("set_")], (
        f"authorship writes must not go through per-row change(): {per_row}"
    )


def test_merge_source_author_prefers_the_from_target_over_ambient(monkeypatch):
    """`--from <name>` merges a store the ambient config is not pointed at, and
    that block can carry its own `author`. Sending the ambient one restamps
    nothing and fails silently — #267 stays reproducible for exactly the caller
    who used `--from`."""
    from witan.cli import migrate as cli_migrate

    monkeypatch.setattr(
        cli_migrate,
        "_named_target",
        lambda name: _StubCfg(author="Personal Machine Name"),
    )

    from witan import config as cfg_mod

    monkeypatch.setattr(cfg_mod, "load", lambda *a, **k: _StubCfg(author="Ambient"))

    assert cli_migrate._merge_source_author("personal") == "Personal Machine Name"
    # A bare source URI is this machine's own store, where ambient IS correct.
    assert cli_migrate._merge_source_author(None) == "Ambient"


def test_merge_source_author_falls_back_when_the_target_sets_none(monkeypatch):
    from witan.cli import migrate as cli_migrate

    monkeypatch.setattr(
        cli_migrate, "_named_target", lambda name: _StubCfg(author=None)
    )

    from witan import config as cfg_mod

    monkeypatch.setattr(cfg_mod, "load", lambda *a, **k: _StubCfg(author="Ambient"))

    assert cli_migrate._merge_source_author("personal") == "Ambient"


def test_claim_authorship_renders_a_bracketed_author_intact(monkeypatch, capsys):
    """#271's boundary rule applies here too: `was`/`now` are stored content.

    Both of Rich's failure modes are covered, because they fail differently and
    an author string can carry either. A lowercase tag-like bracket is dropped
    SILENTLY (`[targets.production]`), gutting the one message whose whole job
    is to show which identity replaces which; a bracketed absolute path parses
    as a closing tag with nothing open and takes the command down with
    MarkupError.

    Note `[MIT]` would NOT reproduce this — Rich's tag pattern does not match
    it, so an uppercase bracket survives unescaped and would make this test
    pass against unescaped code.
    """
    from witan.cli import migrate as cli_migrate

    class _Srv:
        def __init__(self, was_out):
            self.was_out = was_out

        def claim_authorship(self, *, was, apply):
            return {
                "applied": False,
                "was": self.was_out,
                "now": "dev-two",
                "claimed": 2,
                "by_type": {"Memory": 2},
            }

    for author in ("Dev [targets.production] One", "Dev [/var/lib] One"):
        monkeypatch.setattr(
            cli_migrate, "_srv", lambda a=author: _Srv(a), raising=False
        )
        cli_migrate.claim_authorship(author)
        out = capsys.readouterr().out
        assert author in out, f"{author!r} did not survive rendering: {out!r}"


# ── Divergence reporting (tk-witan-migrate-merge-silently-drops-divergent-edi) ──
#
# Newest-record-wins is the right rule and stays the rule. What these cover is
# the case where applying it discards an edit rather than a stale value — both
# stores wrote the same node since they last agreed — which used to land in the
# `kept` bucket, indistinguishable from the thousands of nodes that genuinely
# needed no action.


def _node(node_type, slug, updated_at):
    return {"type": node_type, "data": {"slug": slug, "updated_at": updated_at}}


def _nodes(*rows):
    return {(r["type"], r["data"]["slug"]): r for r in rows}


_LAST_MERGE = {"source_ts": "2026-06-01T00:00:00Z", "target_ts": "2026-06-01T00:00:00Z"}


def test_no_watermark_marks_nothing_diverged():
    """Absent a watermark the answer is "cannot tell", not "nothing diverged".

    Record-level timestamps alone cannot separate a target that is merely ahead
    from one that advanced alongside the source, so guessing would put every
    ordinary collision in a report whose only virtue is that it is short.
    """
    from witan import server as srv

    decisions, _ = srv._reconcile_nodes(
        _nodes(_node("Memory", "mem-a", "2026-06-02T00:00:00Z")),
        _nodes(_node("Memory", "mem-a", "2026-06-03T00:00:00Z")),
    )

    assert decisions[0]["decision"] == "kept-target"
    assert "diverged" not in decisions[0]
    assert srv._decision_counts(decisions)["diverged"] == 0


def test_a_target_only_edit_is_not_divergence():
    """The noise case the watermark exists to exclude.

    The source has not been touched since the last merge, so keeping the
    target's newer row discards nothing. On a shared graph other people write
    constantly, which makes this the common shape of a `kept-target` — reported,
    it would bury the real thing.
    """
    from witan import server as srv

    decisions, _ = srv._reconcile_nodes(
        _nodes(_node("Memory", "mem-a", "2026-05-01T00:00:00Z")),
        _nodes(_node("Memory", "mem-a", "2026-06-05T00:00:00Z")),
        _LAST_MERGE,
    )

    assert decisions[0]["decision"] == "kept-target"
    assert "diverged" not in decisions[0]


def test_both_sides_written_since_the_last_merge_is_reported():
    from witan import server as srv

    decisions, winners = srv._reconcile_nodes(
        _nodes(_node("WorkflowProject", "wp-x", "2026-06-02T00:00:00Z")),
        _nodes(_node("WorkflowProject", "wp-x", "2026-06-03T00:00:00Z")),
        _LAST_MERGE,
    )

    assert decisions[0]["diverged"] is True
    # Reporting only. The rule is unchanged and so is the outcome: the target's
    # row is newer, so it stays and the source row is not written.
    assert decisions[0]["decision"] == "kept-target"
    assert winners == []
    assert srv._decision_counts(decisions)["diverged"] == 1
    # Readable spellings ride along, because these two are the only timestamps
    # a human is asked to compare and an 0.9 export gives them as epoch millis.
    assert decisions[0]["source_at"] == "2026-06-02T00:00:00Z"
    assert decisions[0]["target_at"] == "2026-06-03T00:00:00Z"


def test_epoch_millis_timestamps_are_reported_readably():
    """The representation the deployed graph actually exports.

    Both stores in a real cutover are on omnigraph >= 0.9, so without this the
    divergence report hands you two 13-digit integers to compare by eye.
    """
    from witan import server as srv

    decisions, _ = srv._reconcile_nodes(
        _nodes(_node("WorkflowProject", "wp-x", 1780358400000)),
        _nodes(_node("WorkflowProject", "wp-x", 1780444800000)),
        {"source_ts": 1780272000000, "target_ts": 1780272000000},
    )

    assert decisions[0]["diverged"] is True
    assert decisions[0]["source_at"] == "2026-06-02T00:00:00Z"
    assert decisions[0]["target_at"] == "2026-06-03T00:00:00Z"
    # The raw values stay as exported; the readable pair is added, not swapped.
    assert decisions[0]["source_ts"] == 1780358400000


def test_divergence_is_reported_when_the_source_wins_too():
    """The direction that overwrites the SHARED graph, which is the worse one.

    A divergence resolved in the source's favour discards an edit somebody else
    made in the deployment. It counts as `updated` — a perfectly ordinary-looking
    bucket — so without the mark there is nothing at all to notice.
    """
    from witan import server as srv

    decisions, winners = srv._reconcile_nodes(
        _nodes(_node("WorkflowProject", "wp-x", "2026-06-05T00:00:00Z")),
        _nodes(_node("WorkflowProject", "wp-x", "2026-06-02T00:00:00Z")),
        _LAST_MERGE,
    )

    assert decisions[0]["decision"] == "updated"
    assert decisions[0]["diverged"] is True
    assert len(winners) == 1


def test_diverged_count_cuts_across_the_other_buckets():
    """`diverged` overlaps `updated`/`kept_target` rather than partitioning with
    them — a divergence is still resolved as one of the two."""
    from witan import server as srv

    decisions, _ = srv._reconcile_nodes(
        _nodes(
            _node("Memory", "mem-src-wins", "2026-06-05T00:00:00Z"),
            _node("Memory", "mem-dst-wins", "2026-06-02T00:00:00Z"),
            _node("Memory", "mem-new", "2026-06-05T00:00:00Z"),
        ),
        _nodes(
            _node("Memory", "mem-src-wins", "2026-06-03T00:00:00Z"),
            _node("Memory", "mem-dst-wins", "2026-06-04T00:00:00Z"),
        ),
        _LAST_MERGE,
    )

    counts = srv._decision_counts(decisions)
    assert (counts["added"], counts["updated"], counts["kept_target"]) == (1, 1, 1)
    assert counts["diverged"] == 2
    assert counts["added"] + counts["updated"] + counts["kept_target"] == len(decisions)


def test_the_watermark_covers_the_rows_the_merge_itself_loads():
    """A row this merge writes into the target must not read as "the target
    changed" on the next run.

    The winners land carrying their own (source) timestamps, so a mark taken
    from the pre-merge target alone sits below them — and every row the merge
    added would come back as a divergence next time round.
    """
    from witan import server as srv

    source = _nodes(_node("Memory", "mem-a", "2026-06-05T00:00:00Z"))
    target = _nodes(_node("Memory", "mem-a", "2026-06-01T00:00:00Z"))

    decisions, winners = srv._reconcile_nodes(source, target)
    assert decisions[0]["decision"] == "updated"

    watermark = srv._next_watermark(None, source, target, winners)
    assert watermark["target_ts"] == "2026-06-05T00:00:00Z"

    # Replay: the target now holds what the merge just put there, and the
    # source is untouched. Nothing diverged.
    after, _ = srv._reconcile_nodes(
        source, _nodes(_node("Memory", "mem-a", "2026-06-05T00:00:00Z")), watermark
    )
    assert "diverged" not in after[0]


def test_a_source_clock_ahead_opens_a_blind_window_on_the_target():
    """The one place the per-side clock rule is bent, pinned with its cost.

    Winner timestamps are folded into the target mark so a merge's own rows do
    not read as target edits next run. That mixes the source's clock into the
    target's threshold, so a source running ahead pushes the mark into the
    target's future and hides a genuine target edit made inside the skew.

    Asserting the CURRENT behaviour, not the desired one. If this starts
    reporting True, the trade has been removed (per-record baselines) and the
    docstring in `_next_watermark` should go with it.
    """
    from witan import server as srv

    # Source machine is an hour ahead of the target's clock.
    source = _nodes(_node("Memory", "mem-a", "2026-06-01T12:00:00Z"))
    target = _nodes(_node("Memory", "mem-a", "2026-06-01T10:00:00Z"))
    _, winners = srv._reconcile_nodes(source, target)
    watermark = srv._next_watermark(None, source, target, winners)
    assert watermark["target_ts"] == "2026-06-01T12:00:00Z"  # source's clock

    # A real, independent target edit at 11:30 target-time — after the merge.
    skewed, _ = srv._reconcile_nodes(
        _nodes(_node("Memory", "mem-a", "2026-06-01T12:30:00Z")),
        _nodes(_node("Memory", "mem-a", "2026-06-01T11:30:00Z")),
        watermark,
    )
    assert "diverged" not in skewed[0], "the blind window closed — update the docs"

    # Same shape with the clocks agreeing reports correctly, which is what
    # makes the miss above attributable to skew rather than to the rule.
    source = _nodes(_node("Memory", "mem-b", "2026-06-01T10:00:00Z"))
    target = _nodes(_node("Memory", "mem-b", "2026-06-01T09:00:00Z"))
    _, winners = srv._reconcile_nodes(source, target)
    synced, _ = srv._reconcile_nodes(
        _nodes(_node("Memory", "mem-b", "2026-06-01T12:30:00Z")),
        _nodes(_node("Memory", "mem-b", "2026-06-01T11:30:00Z")),
        srv._next_watermark(None, source, target, winners),
    )
    assert synced[0]["diverged"] is True


def test_the_watermark_accumulates_across_batches():
    """The MCP path splits the source, so no single batch sees its newest row.

    Carrying the running mark forward is what lets the client end up with one
    covering the whole merge without comparing exported timestamps itself —
    including when the newest row is not in the last batch.
    """
    from witan import server as srv

    first = _nodes(_node("Memory", "mem-a", "2026-06-09T00:00:00Z"))
    second = _nodes(_node("Memory", "mem-b", "2026-06-02T00:00:00Z"))

    carried = srv._next_watermark(None, first, {}, [])
    carried = srv._next_watermark(carried, second, {}, [])

    assert carried["source_ts"] == "2026-06-09T00:00:00Z"


def test_each_side_is_compared_against_its_own_mark():
    """Never source-vs-target. The source is a laptop's clock and the target a
    cluster's; comparing across them would read skew as divergence."""
    from witan import server as srv

    # The target's whole history sits "before" the source's, as it would under a
    # few hours of clock skew. Neither side has moved since its own mark.
    decisions, _ = srv._reconcile_nodes(
        _nodes(_node("Memory", "mem-a", "2026-06-01T12:00:00Z")),
        _nodes(_node("Memory", "mem-a", "2026-06-01T02:00:00Z")),
        {"source_ts": "2026-06-01T12:00:00Z", "target_ts": "2026-06-01T02:00:00Z"},
    )

    assert "diverged" not in decisions[0]


def test_watermark_file_round_trips_and_replaces_by_pair(tmp_path, monkeypatch):
    from witan import merge_watermark as mw

    monkeypatch.setenv("WITAN_MERGE_WATERMARKS", str(tmp_path / "marks.json"))

    assert mw.read("/store.omni", "https://witan.example") is None

    assert mw.write(
        "/store.omni", "https://witan.example", {"source_ts": 1, "target_ts": 2}
    )
    entry = mw.read("/store.omni", "https://witan.example")
    assert (entry["source_ts"], entry["target_ts"]) == (1, 2)

    # Re-merging the same pair replaces its mark rather than accumulating one
    # per run, while a different destination keeps its own.
    mw.write("/store.omni", "https://witan.example", {"source_ts": 3, "target_ts": 4})
    mw.write("/store.omni", "/other.omni", {"source_ts": 9, "target_ts": 9})
    assert mw.read("/store.omni", "https://witan.example")["source_ts"] == 3
    assert mw.read("/store.omni", "/other.omni")["source_ts"] == 9
    assert len(mw._load()) == 2


def test_a_corrupt_watermark_file_reads_as_absent(tmp_path, monkeypatch):
    """Fails soft in the direction that loses the report, never the merge.

    A half-written or hand-mangled hint file must not take down a cutover; the
    cost of ignoring it is one run without divergence reporting, and the next
    successful merge rewrites it.
    """
    from witan import merge_watermark as mw

    marks = tmp_path / "marks.json"
    monkeypatch.setenv("WITAN_MERGE_WATERMARKS", str(marks))

    for garbage in ('{"version": 1, "pairs"', "null", "[]", '{"version": 99}'):
        marks.write_text(garbage)
        assert mw.read("/store.omni", "/target.omni") is None

    assert mw.write("/store.omni", "/target.omni", {"source_ts": 1, "target_ts": 1})
    assert mw.read("/store.omni", "/target.omni") is not None


class _FakeMergeProvider:
    """Stands in for either merge provider — the server module or the proxy."""

    remote_url = "https://witan.example/graphs/council"

    def __init__(self):
        self.since_seen = []

    def merge_store(self, source, *, target, dry_run, source_author, since):
        self.since_seen.append(since)
        return {
            "target": self.remote_url,
            # `kept_target: 1` with an empty decision list is a shape a real
            # result cannot have — the counts are derived FROM the decisions —
            # and faking it hid the "this merge carried nothing" early return.
            "decisions": [
                {
                    "type": "Memory",
                    "slug": "mem-collided",
                    "decision": "kept-target",
                    "source_ts": "2026-05-01T00:00:00Z",
                    "target_ts": "2026-06-02T00:00:00Z",
                }
            ],
            "added": 0,
            "updated": 0,
            "kept_target": 1,
            "diverged": 0,
            "rows_loaded": 0,
            "watermark": {
                "source_ts": "2026-06-01T00:00:00Z",
                "target_ts": "2026-06-02T00:00:00Z",
            },
        }


def test_merge_records_its_watermark_and_replays_it_next_run(tmp_path, monkeypatch):
    from witan.cli import migrate as cli_migrate

    monkeypatch.setenv("WITAN_MERGE_WATERMARKS", str(tmp_path / "marks.json"))
    provider = _FakeMergeProvider()
    monkeypatch.setattr(
        cli_migrate, "_merge_destination", lambda to, t: (provider, None)
    )
    monkeypatch.setattr(cli_migrate, "_merge_source_author", lambda f: "pytest")

    cli_migrate._merge("/store.omni", None, False)
    cli_migrate._merge("/store.omni", None, False)

    assert provider.since_seen[0] is None
    assert provider.since_seen[1] == {
        "source_ts": "2026-06-01T00:00:00Z",
        "target_ts": "2026-06-02T00:00:00Z",
    }
    # Exactly the two marks. The stored entry also records which pair it
    # describes, and a merge has no business sending a deployment the local
    # path its rows came from.
    assert "source" not in provider.since_seen[1]


def test_a_dry_run_does_not_record_a_watermark(tmp_path, monkeypatch):
    """The mark describes a target with this merge's winners in it, and a dry
    run wrote none of them. Recording it would tell the next run that everything
    up to here had already been merged."""
    from witan import merge_watermark as mw
    from witan.cli import migrate as cli_migrate

    monkeypatch.setenv("WITAN_MERGE_WATERMARKS", str(tmp_path / "marks.json"))
    provider = _FakeMergeProvider()
    monkeypatch.setattr(
        cli_migrate, "_merge_destination", lambda to, t: (provider, None)
    )
    monkeypatch.setattr(cli_migrate, "_merge_source_author", lambda f: "pytest")

    cli_migrate._merge("/store.omni", None, True)

    assert mw.read("/store.omni", provider.remote_url) is None


def test_the_watermark_key_matches_between_the_read_and_the_write():
    """Both ends of a merge look the destination up the same way.

    The read happens before the call and the write after it, so a key derived
    from the result would silently miss whatever normalization the callee did to
    the address — and every merge would look like a first merge.
    """
    from witan.cli import migrate as cli_migrate

    class _Module:
        client = type("C", (), {"graph_uri": "/configured/graph.omni"})()

    assert (
        cli_migrate._destination_key(_FakeMergeProvider(), None)
        == "https://witan.example/graphs/council"
    )
    assert cli_migrate._destination_key(_Module(), None) == "/configured/graph.omni"
    assert cli_migrate._destination_key(_Module(), "/explicit.omni") == "/explicit.omni"


def test_one_store_spelled_several_ways_shares_one_mark(tmp_path, monkeypatch):
    """A pair keyed by what the caller typed is keyed by the wrong thing.

    `/tmp/g.omni`, `../tmp/g.omni` and `file:///tmp/g.omni` are one store; a
    mark that misses across them measures the next merge against a graph that is
    not the one being merged.
    """
    from witan import merge_watermark as mw

    monkeypatch.setenv("WITAN_MERGE_WATERMARKS", str(tmp_path / "marks.json"))
    store = tmp_path / "g.omni"
    store.mkdir()

    mw.write(str(store), "/target.omni", {"source_ts": 1, "target_ts": 2})

    monkeypatch.chdir(tmp_path)
    for spelling in (str(store), f"file://{store}", "g.omni", "./g.omni"):
        entry = mw.read(spelling, "/target.omni")
        assert entry is not None, f"{spelling!r} missed its own mark"
        assert entry["source_ts"] == 1


def test_a_relative_path_from_two_directories_is_two_stores(tmp_path, monkeypatch):
    """The other half of the same bug: one key must not cover two stores.

    `graph.omni` run from two different working directories names two different
    graphs, and sharing a mark between them reports divergence against somebody
    else's history."""
    from witan import merge_watermark as mw

    monkeypatch.setenv("WITAN_MERGE_WATERMARKS", str(tmp_path / "marks.json"))
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "graph.omni").mkdir()

    monkeypatch.chdir(tmp_path / "a")
    mw.write("graph.omni", "/target.omni", {"source_ts": 1, "target_ts": 1})

    monkeypatch.chdir(tmp_path / "b")
    assert mw.read("graph.omni", "/target.omni") is None


def test_a_watermark_missing_a_side_is_refused_and_reads_as_absent(
    tmp_path, monkeypatch
):
    """The worst possible stored value: truthy, so it suppresses the "cannot
    tell" notice, but unparseable on both sides, so it detects nothing. Silence
    that reads as "nothing diverged" is the one outcome this feature exists to
    prevent."""
    from witan import merge_watermark as mw

    monkeypatch.setenv("WITAN_MERGE_WATERMARKS", str(tmp_path / "marks.json"))

    assert mw.is_usable({"source_ts": 1, "target_ts": 2})
    assert not mw.is_usable({"source_ts": None, "target_ts": None})
    assert not mw.is_usable({"source_ts": 1, "target_ts": None})
    assert not mw.is_usable(None)

    assert (
        mw.write("/s.omni", "/t.omni", {"source_ts": None, "target_ts": None}) is False
    )
    assert mw.read("/s.omni", "/t.omni") is None


def test_a_non_utf8_watermark_file_still_fails_soft(tmp_path, monkeypatch):
    """`read_text` raises UnicodeDecodeError before `json.loads` is reached, and
    that is a ValueError but not an OSError — so it escaped the narrower catch
    and took down a merge from a module whose docstring promises to fail soft."""
    from witan import merge_watermark as mw

    marks = tmp_path / "marks.json"
    monkeypatch.setenv("WITAN_MERGE_WATERMARKS", str(marks))
    marks.write_bytes(b'{"version": 1, "pairs": [\xff\xfe invalid utf-8 ]}')

    assert mw.read("/s.omni", "/t.omni") is None
    assert mw.write("/s.omni", "/t.omni", {"source_ts": 1, "target_ts": 1})
    assert mw.read("/s.omni", "/t.omni") is not None


def test_a_partial_merge_leaves_no_mark_rather_than_a_stale_one(tmp_path, monkeypatch):
    """Batches commit independently, so a merge that dies part-way has already
    put rows in the target. A mark predating those rows reads them as an
    independent target edit and reports divergence on rows nothing but the
    failed merge ever wrote."""
    from witan import merge_watermark as mw
    from witan.cli import migrate as cli_migrate

    monkeypatch.setenv("WITAN_MERGE_WATERMARKS", str(tmp_path / "marks.json"))
    mw.write("/s.omni", "/t.omni", {"source_ts": 1, "target_ts": 1})

    class _Exploding:
        remote_url = "/t.omni"

        def merge_store(self, source, *, target, dry_run, source_author, since):
            # Stands in for a merge that commits some batches and then dies.
            raise RuntimeError("data tier went away mid-merge")

    monkeypatch.setattr(
        cli_migrate, "_merge_destination", lambda to, t: (_Exploding(), None)
    )
    monkeypatch.setattr(cli_migrate, "_merge_source_author", lambda f: "pytest")

    with pytest.raises(SystemExit):
        cli_migrate._merge("/s.omni", None, False)

    assert mw.read("/s.omni", "/t.omni") is None


def test_a_dry_run_keeps_the_standing_mark(tmp_path, monkeypatch):
    """Retiring the mark is for runs that WRITE. A dry run commits nothing, so
    the standing mark still describes the target accurately and throwing it away
    would blind the next real merge for no reason."""
    from witan import merge_watermark as mw
    from witan.cli import migrate as cli_migrate

    monkeypatch.setenv("WITAN_MERGE_WATERMARKS", str(tmp_path / "marks.json"))
    mw.write("/s.omni", _FakeMergeProvider.remote_url, {"source_ts": 1, "target_ts": 1})

    provider = _FakeMergeProvider()
    monkeypatch.setattr(
        cli_migrate, "_merge_destination", lambda to, t: (provider, None)
    )
    monkeypatch.setattr(cli_migrate, "_merge_source_author", lambda f: "pytest")

    cli_migrate._merge("/s.omni", None, True)

    assert mw.read("/s.omni", _FakeMergeProvider.remote_url) is not None


def test_a_first_dry_run_does_not_promise_the_next_merge_can_report(capsys):
    """A dry run records no mark, so the real merge after it is blind too. The
    earliest run that can report is the one after that."""
    from witan.cli import migrate as cli_migrate

    result = {"decisions": [], "updated": 0, "kept_target": 3}

    cli_migrate._report_divergence(result, None, dry_run=True)
    dry = capsys.readouterr().out
    assert "blind" in dry

    cli_migrate._report_divergence(result, None, dry_run=False)
    real = capsys.readouterr().out
    assert "the next one will report" in real


def test_divergence_report_names_the_slugs_and_which_side_was_kept(capsys):
    """Naming the slugs is the deliverable — the manual reconcile takes minutes
    once you know where to look, and the summary counts never tell you."""
    from witan.cli import migrate as cli_migrate

    result = {
        "decisions": [
            {
                "type": "WorkflowProject",
                "slug": "wp-witan-multi-user",
                "decision": "kept-target",
                "source_ts": "2026-08-19T19:49:00Z",
                "target_ts": "2026-08-19T22:06:00Z",
                "diverged": True,
            },
            {
                "type": "Memory",
                "slug": "mem-untouched",
                "decision": "kept-target",
                "source_ts": "2026-01-01T00:00:00Z",
                "target_ts": "2026-06-01T00:00:00Z",
            },
        ]
    }

    cli_migrate._report_divergence(
        result, {"merged_at": "2026-08-19T19:46:00Z"}, dry_run=True
    )
    out = capsys.readouterr().out

    assert "wp-witan-multi-user" in out
    assert "kept target" in out
    assert "mem-untouched" not in out


def test_a_first_merge_says_it_cannot_report_divergence(capsys):
    """Silence would read as "nothing diverged", which is the exact confusion
    this whole feature exists to remove."""
    from witan.cli import migrate as cli_migrate

    cli_migrate._report_divergence(
        {"decisions": [], "updated": 0, "kept_target": 3}, None, dry_run=True
    )

    assert "watermark" in capsys.readouterr().out


def test_a_first_merge_that_only_adds_says_nothing(capsys):
    """The note is about collisions it cannot judge. With none, it is noise on
    the run that needs it least — a fresh cutover into an empty graph."""
    from witan.cli import migrate as cli_migrate

    cli_migrate._report_divergence(
        {"decisions": [], "updated": 0, "kept_target": 0}, None, dry_run=True
    )

    assert capsys.readouterr().out == ""


def test_a_merge_that_carried_nothing_does_not_warn_about_the_deployment(
    tmp_path, monkeypatch, capsys
):
    """An empty source reports no mark because there was nothing to mark, not
    because the deployment is too old. Warning would send someone hunting a
    problem that isn't there."""
    from witan.cli import migrate as cli_migrate

    monkeypatch.setenv("WITAN_MERGE_WATERMARKS", str(tmp_path / "marks.json"))
    cli_migrate._record_watermark(
        "/store.omni", "/target.omni", {"decisions": [], "rows_loaded": 0}
    )

    assert capsys.readouterr().out == ""


def test_a_merge_that_moved_rows_but_got_no_mark_warns(tmp_path, monkeypatch, capsys):
    """The pre-0.29.0 deployment case. A merge that plainly did something and
    still returned no mark means the next one cannot report divergence, and
    saying nothing would leave that to be discovered by not being told."""
    from witan.cli import migrate as cli_migrate

    monkeypatch.setenv("WITAN_MERGE_WATERMARKS", str(tmp_path / "marks.json"))
    cli_migrate._record_watermark(
        "/store.omni",
        "/target.omni",
        {"decisions": [{"slug": "mem-a"}], "rows_loaded": 1},
    )

    # A short fragment: the console hard-wraps to the terminal width, so a
    # longer phrase can arrive with a newline through the middle of it.
    assert "No usable merge watermark" in capsys.readouterr().out


@requires_omnigraph
def test_merge_reports_divergence_against_two_real_stores(server, tmp_path):
    """The 2026-08-19 incident, replayed against actual stores.

    Both sides advance after a merge; the second merge resolves the collision
    newest-record-wins and, before this, reported the discarded edit as `kept` —
    the same bucket as every node that needed nothing. The point of the assert
    is not that a count went up but that the losing slug is now nameable.
    """
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import server as srv

    slug = "mem-diverged-d1d1d1"
    source = graph_mod.OmnigraphClient(
        _init_store(tmp_path / "source.omni"), cfg_mod.load().queries_dir
    )
    _insert_memory(
        source, slug=slug, content="agreed", updated_at="2026-06-01T00:00:00Z"
    )

    first = srv.merge_store(source.graph_uri)
    assert first["diverged"] == 0
    watermark = first["watermark"]
    assert watermark["source_ts"] and watermark["target_ts"]

    # Both stores are written after that merge — the shape a merge is most
    # likely to be run in, since writes going somewhere they should not is
    # what prompts one.
    _insert_memory(
        source, slug=slug, content="edited locally", updated_at="2026-06-02T00:00:00Z"
    )
    _insert_memory(
        srv.client,
        slug=slug,
        content="edited in the graph",
        updated_at="2026-06-03T00:00:00Z",
    )

    blind = srv.merge_store(source.graph_uri, dry_run=True)
    told = srv.merge_store(source.graph_uri, dry_run=True, since=watermark)

    # Same decision either way; the difference is entirely whether the loss is
    # visible. Without the watermark it is one of the `kept_target` rows.
    assert blind["kept_target"] == told["kept_target"] == 1
    assert blind["diverged"] == 0
    assert told["diverged"] == 1
    assert [d["slug"] for d in told["decisions"] if d.get("diverged")] == [slug]
