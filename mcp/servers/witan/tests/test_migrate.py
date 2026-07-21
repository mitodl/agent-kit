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


def test_parse_ts_compares_naive_and_aware_without_raising():
    """omnigraph's own export strips the offset witan writes down to a naive
    string — an aware value must still compare against a naive one without
    datetime's usual TypeError."""
    from witan import server as srv

    naive = srv._parse_ts("2026-01-01T12:00:00")
    aware_same_instant = srv._parse_ts("2026-01-01T12:00:00+00:00")

    assert naive == aware_same_instant
