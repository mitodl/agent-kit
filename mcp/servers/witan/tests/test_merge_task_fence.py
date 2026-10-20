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
