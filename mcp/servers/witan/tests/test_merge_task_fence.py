"""A merge's writes to tasks the target already holds are fenced.

A merge exports the target, reconciles, then writes the winners, and a
`task_claim` can land in between: `store_merge` holds no lock across the two,
and `merge_store`'s lock is a no-op on a served target. A winning source row
loaded after that claim put back the source's status, assignee and
claimed_at, and a second claimer was then granted the task. These tests land
the claim in that window.
"""

import copy
import json
from contextlib import nullcontext

import pytest

from witan import server as srv

from .conftest import requires_omnigraph
from .test_migrate import _export_rows
from .test_tasks import _after_first_read, _rival_claim

#: Newer than anything the target can write while a test runs, so
#: newest-record-wins alone would still pick the source over a claim. Only the
#: claim check keeps the target's row.
_FAR_FUTURE = "2099-01-01T00:00:00Z"


def _source_copy(slug: str, **changes) -> dict:
    """The target's export row for ``slug``, as a source store edited later
    would hold it."""
    [row] = [
        r
        for r in _export_rows(srv.client)
        if r.get("type") == "Task" and r["data"]["slug"] == slug
    ]
    row = copy.deepcopy(row)
    row["data"].update({"updated_at": _FAR_FUTURE, **changes})
    return row


def _claim_after_target_export(server, monkeypatch, slug: str, holder: str) -> dict:
    """Grant ``holder`` the task right after the next export of the configured
    store, the window between a merge's export and its write."""
    real_export = srv.OmnigraphClient.export_to
    granted: dict = {}

    def export_then_claim(self, path, *, label="export"):
        real_export(self, path, label=label)
        if not granted and self.graph_uri == srv.client.graph_uri:
            granted.update(server.task_claim(slug, assignee=holder))

    monkeypatch.setattr(srv.OmnigraphClient, "export_to", export_then_claim)
    return granted


@requires_omnigraph
def test_store_merge_keeps_a_claim_landing_between_export_and_write(
    server, monkeypatch
):
    t = server.task_create(title="claimed-mid-merge", description="x")
    source = _source_copy(t["slug"], description="edited in the source")
    granted = _claim_after_target_export(server, monkeypatch, t["slug"], "agentA")

    result = srv.store_merge([source])

    assert granted["claimed"] is True
    row = server.task_get(t["slug"])
    assert row["status"] == "in_progress"
    assert row["assignee"] == "agentA"
    assert row["description"] == "x"
    b = server.task_claim(t["slug"], assignee="agentB")
    assert b["claimed"] is False
    assert b["held_by"] == "agentA"
    [decision] = result["decisions"]
    assert decision["decision"] == "kept-target"
    assert decision["fenced"] == "claim_changed"
    assert (result["updated"], result["kept_target"], result["rows_loaded"]) == (
        0,
        1,
        0,
    )


@requires_omnigraph
def test_merge_store_keeps_a_claim_landing_between_export_and_write(
    server, monkeypatch, tmp_path
):
    """The in-process path (`witan migrate merge --target <served store>`).

    The suite's store is local, so its lock is replaced by the no-op a served
    store has; held, it would block the claim's write on another thread."""
    t = server.task_create(title="claimed-mid-local-merge", description="x")
    export = tmp_path / "source.jsonl"
    export.write_text(
        json.dumps(_source_copy(t["slug"], description="edited in the source")) + "\n"
    )
    monkeypatch.setattr(
        srv.OmnigraphClient, "hold_write_lock", lambda self: nullcontext()
    )
    granted = _claim_after_target_export(server, monkeypatch, t["slug"], "agentA")

    result = srv.merge_store(str(export))

    assert granted["claimed"] is True
    [decision] = result["decisions"]
    assert decision["decision"] == "kept-target"
    assert decision["fenced"] == "claim_changed"
    assert result["rows_loaded"] == 0
    assert result["batches_applied"] == result["batches"] == 1
    row = server.task_get(t["slug"])
    assert row["assignee"] == "agentA"
    assert row["description"] == "x"


@requires_omnigraph
def test_a_merged_task_is_fenced_on_the_read_it_decided_on(server, monkeypatch):
    """The claim lands after the merge's fenced re-read, not before it, so the
    claim check on that read passes. The store refuses the write's stale
    precondition, and the retry's read sees the claim and keeps it."""
    t = server.task_create(title="claimed-after-the-fenced-read", description="x")
    source = _source_copy(t["slug"], description="edited in the source")
    state = _after_first_read(srv, monkeypatch, lambda: _rival_claim(srv, t["slug"]))

    result = srv.store_merge([source])

    assert state["conflicts"] == 1, "the store did not refuse the stale write"
    [decision] = result["decisions"]
    assert decision["fenced"] == "claim_changed"
    row = server.task_get(t["slug"])
    assert row["assignee"] == "agentB"
    assert row["description"] == "x"


@requires_omnigraph
def test_a_target_edit_after_the_export_is_reconciled_again(server, monkeypatch):
    """An edit that is not a claim still moves `updated_at`. The fenced read
    re-runs newest-record-wins against it, so a source row older than that
    edit no longer wins."""
    t = server.task_create(title="edited-mid-merge", description="x")
    source = _source_copy(t["slug"], description="from the source")
    source["data"]["updated_at"] = srv.now_iso()
    real_export = srv.OmnigraphClient.export_to
    edited = {}

    def export_then_edit(self, path, *, label="export"):
        real_export(self, path, label=label)
        if not edited:
            edited["row"] = srv._update_task(t["slug"], {"description": "target"})

    monkeypatch.setattr(srv.OmnigraphClient, "export_to", export_then_edit)

    result = srv.store_merge([source])

    [decision] = result["decisions"]
    assert decision["decision"] == "kept-target"
    assert decision["fenced"] == "target_newer"
    assert server.task_get(t["slug"])["description"] == "target"


@requires_omnigraph
def test_a_winning_task_collision_is_written_whole(server, monkeypatch):
    """With nothing landing in between, the source row is written as a load
    would have written it: every field, its own timestamps, and the restamped
    author."""
    t = server.task_create(title="merged-untouched", description="x")
    before = server.task_get(t["slug"])
    source = _source_copy(
        t["slug"],
        description="edited in the source",
        status="closed",
        resolution="done elsewhere",
        closed_at="2098-12-31T00:00:00Z",
        tags=["merged"],
    )
    monkeypatch.setattr(srv, "_current_author", lambda: "me@example.org")

    result = srv.store_merge([source], claim_from_author=before["author"])

    [decision] = result["decisions"]
    assert decision["decision"] == "updated"
    assert "fenced" not in decision
    assert result["rows_loaded"] == 1
    assert result["authorship_claimed"] == 1
    row = server.task_get(t["slug"])
    assert row["description"] == "edited in the source"
    assert row["status"] == "closed"
    assert row["resolution"] == "done elsewhere"
    assert row["tags"] == ["merged"]
    assert row["author"] == "me@example.org"
    assert srv._parse_ts(row["updated_at"]) == srv._parse_ts(_FAR_FUTURE)
    assert srv._parse_ts(row["closed_at"]) == srv._parse_ts("2098-12-31T00:00:00Z")
    assert srv._parse_ts(row["created_at"]) == srv._parse_ts(before["created_at"])


def _collisions(server, count: int) -> tuple[list[str], list[dict]]:
    """``count`` tasks in the target, and a newer source copy of each."""
    slugs = [
        server.task_create(title=f"collision {i}", description="x")["slug"]
        for i in range(count)
    ]
    exported = {
        r["data"]["slug"]: r
        for r in _export_rows(srv.client)
        if r.get("type") == "Task"
    }
    rows = []
    for slug in slugs:
        row = copy.deepcopy(exported[slug])
        row["data"].update(updated_at=_FAR_FUTURE, description="from the source")
        rows.append(row)
    return slugs, rows


def _fenced_writes(monkeypatch) -> list[list[str]]:
    """The slugs of each fenced composed write, in order, attempts included."""
    real = srv.OmnigraphClient.change_many
    seen: list[list[str]] = []

    def record(self, steps, **kwargs):
        if kwargs.get("if_commit") is not None and steps[0][1] == "merge_task":
            seen.append([params["slug"] for _, _, params in steps])
        return real(self, steps, **kwargs)

    monkeypatch.setattr(srv.OmnigraphClient, "change_many", record)
    return seen


@requires_omnigraph
def test_collisions_in_one_batch_are_one_composed_commit(server, monkeypatch):
    slugs, rows = _collisions(server, 3)
    writes = _fenced_writes(monkeypatch)

    result = srv.store_merge(rows)

    assert writes == [slugs]
    assert result["updated"] == result["rows_loaded"] == 3
    for slug in slugs:
        assert server.task_get(slug)["description"] == "from the source"


@requires_omnigraph
def test_collisions_past_one_commit_go_in_several_fenced_batches(
    server, monkeypatch, tmp_path
):
    """The in-process merge has no gateway budget, so it writes every batch."""
    slugs, rows = _collisions(server, 5)
    export = tmp_path / "source.jsonl"
    export.write_text("".join(json.dumps(row) + "\n" for row in rows))
    monkeypatch.setattr(srv.merge_bounds, "TASKS_PER_COMMIT", 2)
    writes = _fenced_writes(monkeypatch)

    result = srv.merge_store(str(export))

    assert writes == [slugs[0:2], slugs[2:4], slugs[4:5]]
    assert result["batches"] == result["batches_applied"] == 3
    assert result["updated"] == result["rows_loaded"] == 5
    for slug in slugs:
        assert server.task_get(slug)["description"] == "from the source"


def _new_memory_row(tmp_path) -> dict:
    """An export row for a Memory the target does not hold, so it is loaded."""
    from witan import config as cfg_mod
    from witan import graph as graph_mod

    from .test_migrate import _init_store, _insert_memory

    source = graph_mod.OmnigraphClient(
        _init_store(tmp_path / "memory-source.omni"), cfg_mod.load().queries_dir
    )
    _insert_memory(
        source, slug="mem-new-in-merge-aaaaaa", content="new", updated_at=_FAR_FUTURE
    )
    [row] = _export_rows(source)
    return row


@requires_omnigraph
def test_store_merge_refuses_more_collisions_than_one_commit(
    server, monkeypatch, tmp_path
):
    """One call writes at most one fenced commit, so a batch needing more is
    refused before its load, not part-way through. The new Memory row is what
    the load would have written first."""
    slugs, rows = _collisions(server, 3)
    monkeypatch.setattr(srv.merge_bounds, "TASKS_PER_COMMIT", 2)

    def no_load(self, records, mode="merge"):
        raise AssertionError("the batch was loaded before it was refused")

    monkeypatch.setattr(srv.OmnigraphClient, "load_batch", no_load)

    with pytest.raises(srv.MergeBatchTooLarge, match="Nothing was written"):
        srv.store_merge([_new_memory_row(tmp_path), *rows])
    for slug in slugs:
        assert server.task_get(slug)["description"] == "x"


@requires_omnigraph
def test_an_old_client_batch_is_refused_with_the_version_that_splits(server, tmp_path):
    """A client from before the split sends `chunk_records(rows,
    MCP_LOAD_MAX_BYTES)` whole, so 33 newer copies of tasks the deployment
    holds arrive in one call. The refusal is a `Refusal` (a warning, not a
    Sentry error), names the version to upgrade to, and a dry run gets it
    too, so it predicts the real run."""
    from witan_core.chunking import MCP_LOAD_MAX_BYTES, chunk_records
    from witan_core.refusal import Refusal

    _, rows = _collisions(server, srv.merge_bounds.TASKS_PER_COMMIT + 1)
    [batch] = list(
        chunk_records([_new_memory_row(tmp_path), *rows], MCP_LOAD_MAX_BYTES)
    )

    for dry_run in (True, False):
        with pytest.raises(srv.MergeBatchTooLarge) as excinfo:
            srv.store_merge(batch, dry_run=dry_run)
        assert isinstance(excinfo.value, Refusal)
        message = str(excinfo.value)
        assert f"witan-council {srv.merge_bounds.SPLITTING_CLIENT_VERSION}" in message
        assert "uv tool upgrade" in message
    assert server.memory_get("mem-new-in-merge-aaaaaa") is None


@requires_omnigraph
def test_contention_part_way_reports_what_the_merge_wrote(
    server, monkeypatch, tmp_path
):
    """The load and the first task batch commit; the second batch runs out of
    attempts. The partial report counts exactly what landed, and the WRITTEN
    accounting shows the two tasks that did not."""
    from witan import graph as graph_mod
    from witan import merge_report

    slugs, rows = _collisions(server, 3)
    export = tmp_path / "source.jsonl"
    export.write_text(
        "".join(json.dumps(row) + "\n" for row in [_new_memory_row(tmp_path), *rows])
    )
    monkeypatch.setattr(srv.merge_bounds, "TASKS_PER_COMMIT", 1)
    monkeypatch.setattr(srv, "_task_write_pause", lambda _attempt: None)
    real = srv.OmnigraphClient.change_many

    def refuse_the_second(self, steps, **kwargs):
        if kwargs.get("if_commit") is not None and steps[0][2]["slug"] == slugs[1]:
            raise graph_mod.OmnigraphConflict("refused")
        return real(self, steps, **kwargs)

    monkeypatch.setattr(srv.OmnigraphClient, "change_many", refuse_the_second)

    with pytest.raises(srv.WriteContention) as excinfo:
        srv.merge_store(str(export))

    partial = merge_report.partial_of(excinfo.value)
    assert partial["batches"] == 4
    assert partial["batches_applied"] == 2
    assert partial["rows_loaded"] == 2
    written = merge_report.accounting(partial)
    assert written["expected_loaded"] == 4
    assert written["unwritten"] == 2
    assert server.task_get(slugs[0])["description"] == "from the source"
    assert server.task_get(slugs[1])["description"] == "x"
    assert server.task_get(slugs[2])["description"] == "x"


def test_the_splitting_version_is_not_ahead_of_this_release():
    """The refusal tells old clients which version splits. Naming one later
    than the package that ships the split would send them to a release that
    does not exist."""
    import importlib.metadata

    def parts(version):
        return tuple(int(p) for p in version.split(".")[:3])

    installed = importlib.metadata.version("witan-council")
    assert parts(srv.merge_bounds.SPLITTING_CLIENT_VERSION) <= parts(installed)


def test_the_splitting_version_is_the_release_that_ships_the_split():
    """The reverse of the check above: a constant left behind by a later
    rebase and re-bump would name a release that does not split, and still
    pass it. The release the refusal names has to be the one whose changelog
    introduces the refusal."""
    from pathlib import Path

    changelog = (Path(__file__).parents[1] / "CHANGELOG.md").read_text()
    version = srv.merge_bounds.SPLITTING_CLIENT_VERSION
    heading = f"## [{version}] - "
    assert heading in changelog
    section = changelog.split(heading, 1)[1].split("\n## [", 1)[0]
    assert "MergeBatchTooLarge" in section


def test_split_by_task_budget_bounds_task_rows_and_keeps_order(monkeypatch):
    monkeypatch.setattr(srv.merge_bounds, "TASKS_PER_COMMIT", 2)

    def task(slug):
        return {"type": "Task", "data": {"slug": slug}}

    memory = {"type": "Memory", "data": {"slug": "mem-a"}}
    edge = {"edge": "Blocks", "from": "a", "to": "b"}
    rows = [task("a"), memory, task("b"), task("c"), edge]

    assert srv.merge_bounds.split_by_task_budget(rows) == [
        [task("a"), memory, task("b")],
        [task("c"), edge],
    ]
    big = task("d" * 100)
    assert srv.merge_bounds.split_by_task_budget(
        [task("a"), big, task("e")], max_bytes=srv.merge_bounds.task_cost(big)
    ) == [[task("a")], [big], [task("e")]]


def test_split_reads_the_byte_budget_when_called(monkeypatch):
    """A row over the budget on its own still gets a part, alone, and the
    budget is the module's value at call time, not at import."""

    def task(slug):
        return {"type": "Task", "data": {"slug": slug}}

    big = task("d" * 100)
    monkeypatch.setattr(
        srv.merge_bounds, "TASK_BYTES_PER_COMMIT", srv.merge_bounds.task_cost(big) - 1
    )

    assert srv.merge_bounds.split_by_task_budget([task("a"), big, task("e")]) == [
        [task("a")],
        [big],
        [task("e")],
    ]
