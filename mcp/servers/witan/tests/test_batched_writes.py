"""Each multi-row write path must cost ONE commit, not one per row.

These count `omnigraph mutate` invocations against the real store rather than
asserting on the graph's contents — the contents are already covered elsewhere,
and what regresses silently here is the commit COUNT. Every mutate is a Lance
version; a store accumulating one per row is what drove point reads to 167ms in
the PR #180 spike.
"""

import subprocess

import pytest

from .conftest import requires_omnigraph

pytestmark = requires_omnigraph


@pytest.fixture
def commits(monkeypatch):
    """Count `omnigraph mutate` subprocess invocations while in scope."""
    real_run = subprocess.run
    counter = {"n": 0}

    def counting_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and "mutate" in cmd:
            counter["n"] += 1
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)

    class Counter:
        def reset(self):
            counter["n"] = 0

        @property
        def count(self):
            return counter["n"]

    return Counter()


def test_memory_store_with_tags_is_one_commit(server, commits):
    commits.reset()
    server.memory_store(
        kind="pattern", title="batched", content="c", tags=["a", "b", "c"]
    )
    # node + 3 Topic inserts + 3 Tagged edges — 7 mutates before batching
    assert commits.count == 1


def test_memory_update_tags_is_one_commit_beyond_the_update(server, commits):
    stored = server.memory_store(kind="pattern", title="upd", content="c")
    commits.reset()
    server.memory_update(slug=stored["slug"], tags=["d", "e", "f"])
    # the field update, then ONE commit for all 3 topics + 3 edges (was 7)
    assert commits.count == 2


def test_memory_link_tagged_creating_a_topic_is_one_commit(server, commits):
    stored = server.memory_store(kind="pattern", title="lnk", content="c")
    commits.reset()
    result = server.memory_link(
        from_slug=stored["slug"], to_slug="brand-new:topic", kind="tagged"
    )
    assert result["linked"] is True
    # Topic insert + Tagged edge in one body (was 2)
    assert commits.count == 1


def test_task_create_with_every_edge_kind_is_one_commit(server, commits):
    project = server.workflow_project_create(title="P", description="d")
    first = server.task_create(title="blocker one", description="d")
    second = server.task_create(title="blocker two", description="d")
    commits.reset()
    created = server.task_create(
        title="rich task",
        description="d",
        project_slug=project["slug"],
        parent=first["slug"],
        blocked_by=[first["slug"], second["slug"]],
        discovered_from=[second["slug"]],
    )
    # node + BelongsTo + ParentOf + 2 Blocks + DiscoveredFrom — 6 before
    assert commits.count == 1
    assert created["status"] == "blocked"


def test_bare_task_create_still_takes_the_single_step_path(server, commits):
    commits.reset()
    server.task_create(title="bare", description="d")
    assert commits.count == 1


def test_migrate_topics_batches_the_whole_backfill(server, commits, monkeypatch):
    # Seed the legacy shape the backfill exists for: memories carrying tag
    # STRINGS but no Topic nodes or Tagged edges. Going through memory_store
    # would dual-write them and leave nothing to migrate.
    now = "2026-08-05T00:00:00Z"
    seed = [
        (
            "mutations.gq",
            "insert_memory",
            {
                "slug": f"pat-legacy-{i}",
                "kind": "pattern",
                "title": f"t{i}",
                "content": "c",
                "repo": None,
                "language": None,
                "category": None,
                "severity": None,
                "author": "pytest",
                "tags": [f"tag-{i}", "shared"],
                "symbol_refs": None,
                "confidence": None,
                "created_at": now,
                "updated_at": now,
            },
        )
        for i in range(6)
    ]
    server._module.client.change_many(seed)

    commits.reset()
    result = server.migrate_topics()
    # 7 topics (6 distinct + "shared") + 12 edges = 19 writes, one commit
    assert result["memories_scanned"] == 6
    assert result["topics_created"] == 7
    assert result["edges_created"] == 12
    assert commits.count == 1


def test_migrate_topics_flushes_in_bounded_chunks(server, commits, monkeypatch):
    # The composed query rides in argv, so the backfill must not build one
    # unbounded body no matter how much legacy data it finds.
    monkeypatch.setattr(server._module, "_MIGRATE_BATCH_SIZE", 5)
    now = "2026-08-05T00:00:00Z"
    server._module.client.change_many(
        [
            (
                "mutations.gq",
                "insert_memory",
                {
                    "slug": f"pat-chunk-{i}",
                    "kind": "pattern",
                    "title": f"t{i}",
                    "content": "c",
                    "repo": None,
                    "language": None,
                    "category": None,
                    "severity": None,
                    "author": "pytest",
                    "tags": [f"chunk-tag-{i}"],
                    "symbol_refs": None,
                    "confidence": None,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            for i in range(6)
        ]
    )
    commits.reset()
    result = server.migrate_topics()
    assert result["edges_created"] == 6
    # 6 memories x (1 topic + 1 edge) = 12 steps. The threshold is checked
    # BETWEEN memories, so it flushes at 6 steps rather than exactly 5 — two
    # commits, not one, and not the twelve this would have been unbatched.
    assert commits.count == 2


def test_migrate_topics_is_idempotent_and_writes_nothing_on_a_rerun(server, commits):
    server.memory_store(kind="pattern", title="already", content="c", tags=["x"])
    server.migrate_topics()
    commits.reset()
    result = server.migrate_topics()
    assert result["topics_created"] == 0
    assert result["edges_created"] == 0
    assert commits.count == 0
