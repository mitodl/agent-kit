"""Tests for the schema/data migration commands."""

import subprocess
from pathlib import Path

from .conftest import SCHEMA, requires_omnigraph


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


@requires_omnigraph
def test_find_pre_upgrade_binary_skips_the_current_one():
    import shutil

    from witan import server as srv

    current = shutil.which("omnigraph")
    assert srv._find_pre_upgrade_binary(current) is None


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


def test_parse_export_raises_on_missing_type_field(tmp_path):
    from witan import server as srv

    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"data": {"slug": "mem-x-aaaaaa"}}\n')
    try:
        srv._parse_export(bad)
    except RuntimeError as exc:
        assert "missing a 'type'" in str(exc)
    else:
        raise AssertionError("expected RuntimeError for a row missing 'type'")
