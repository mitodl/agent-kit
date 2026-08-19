"""How many Lance commits one tool call costs.

★ THESE TESTS PIN A COST, NOT A BEHAVIOUR, and that is deliberate — the cost is
invisible from every other test in this suite. A tool that writes its rows in
four commits instead of one returns exactly the same data and passes every
correctness assertion; the only signal is latency against a real store, which
nothing in CI measures.

The numbers that make this worth a test: against the deployed store each commit
costs ~3.5-4s (a full Lance commit cycle against S3), the shared graph
serialises writes at ~0.25/s, and ToolHive cuts every tool call at a hardcoded
30 seconds. So `workflow_session_start` at four commits spent 14-16s of its
budget before any other user contended for anything — and it is the FIRST call
every agent session makes. See
tk-batch-the-hot-witan-write-paths-one-tool-call-is-a8227e.

Counting `change`/`change_many` invocations is the honest proxy: the commit unit
is the `mutate` invocation, not the statement, so N calls are N Lance versions
however many rows each carries.
"""

import subprocess

import pytest

from .conftest import requires_omnigraph

REPO = "https://github.com/test/repo"


def _git(base, *args):
    subprocess.run(
        ["git", "-C", str(base), *args], check=True, capture_output=True, text=True
    )


def _git_repo(path):
    path.mkdir(exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
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
def commits(monkeypatch):
    """Count the commits a block of tool calls issues.

    Wraps the live client rather than faking it: these tests assert the write
    actually lands as well as how it is packaged, and a fake would let a step
    list that omnigraph rejects still 'pass'.
    """
    from witan import server as srv

    calls: list[str] = []
    real_change = srv.client.change
    real_change_many = srv.client.change_many
    # ★ `change_many` with exactly ONE step delegates to `change`
    # (witan_core.omnigraph.change_many), so a naive wrapper counts that batch
    # twice and every assertion here silently doubles. Depth-guarding is what
    # makes the fixture measure COMMITS rather than method calls.
    inside = {"batch": 0}

    def counting_change(query_file, query_name, *args, **kwargs):
        # Record the mutation NAME, not just a tally: when this fails, "which
        # write escaped the batch" is the whole question, and a bare count
        # sends the reader back to the source to guess.
        if not inside["batch"]:
            calls.append(query_name)
        return real_change(query_file, query_name, *args, **kwargs)

    def counting_change_many(steps, *args, **kwargs):
        calls.append("+".join(name for _file, name, _params in steps))
        inside["batch"] += 1
        try:
            return real_change_many(steps, *args, **kwargs)
        finally:
            inside["batch"] -= 1

    monkeypatch.setattr(srv.client, "change", counting_change)
    monkeypatch.setattr(srv.client, "change_many", counting_change_many)
    return calls


@requires_omnigraph
def test_session_start_on_a_branch_is_one_commit(
    server, tmp_path, monkeypatch, commits
):
    """★ The worst path before this: insert+edge, the project's repo set, the
    CodeBranch upsert, and the ForProject edge were four separate commits."""
    from witan import server as srv

    base = _git_repo(tmp_path / "r")
    _git(base, "checkout", "-q", "-b", "feature/batching")
    monkeypatch.chdir(base)

    project = server.workflow_project_create(title="batching", description="d")
    commits.clear()  # project creation is its own call, measured separately

    handle = server.workflow_session_start(
        project_slug=project["slug"], session_id="sess-batch-1", phase="implementation"
    )

    assert len(commits) == 1, f"expected one commit, got {commits}"
    # …and it really wrote everything, not just the session.
    assert handle["session_slug"]
    branch_slug = srv._code_branch_slug(REPO, "feature/batching")
    assert srv.client.read("read.gq", "get_code_branch", {"slug": branch_slug})
    assert srv.client.read(
        "read.gq",
        "code_branch_for_project_edge",
        {"branch_slug": branch_slug, "project_slug": project["slug"]},
    ), "the ForProject edge must be in that same commit, not dropped"


@requires_omnigraph
def test_re_entrant_session_start_costs_one_commit(
    server, tmp_path, monkeypatch, commits
):
    """The re-entrant path — a hook retry, a reconnect — is three commits' worth
    of work collapsed to one.

    NOT zero, and deliberately so: `touch_code_branch` bumps the branch's
    `updated_at` on every call, which is what tells the branch reaper the branch
    is still live. The session meta and the project's repo set are both
    unchanged here and contribute nothing.
    """
    base = _git_repo(tmp_path / "r")
    _git(base, "checkout", "-q", "-b", "feature/reentrant")
    monkeypatch.chdir(base)

    project = server.workflow_project_create(title="reentrant", description="d")
    server.workflow_session_start(
        project_slug=project["slug"], session_id="sess-batch-2", phase="implementation"
    )
    commits.clear()

    again = server.workflow_session_start(
        project_slug=project["slug"], session_id="sess-batch-2", phase="implementation"
    )

    assert again["existed"] is True
    assert commits == ["touch_code_branch"], f"one liveness touch only: {commits}"


@requires_omnigraph
def test_task_update_with_a_parent_is_one_commit(server, commits):
    """It used to be three: update the row, write the ParentOf edge, then update
    the row AGAIN just to record `parent_slug`. A reader between those commits
    saw a task parented one way and not the other."""
    from witan import server as srv

    parent = server.task_create(title="parent", description="p", repo=REPO)
    child = server.task_create(title="child", description="c", repo=REPO)
    commits.clear()

    updated = server.task_update(slug=child["slug"], parent=parent["slug"])

    assert len(commits) == 1, f"expected one commit, got {commits}"
    # Both encodings of "has a parent" land together or not at all.
    assert updated["parent_slug"] == parent["slug"]
    kids = srv.client.read(
        "read.gq", "list_tasks_by_parent", {"parent_slug": parent["slug"]}
    )
    assert child["slug"] in {row["slug"] for row in kids}


@requires_omnigraph
def test_a_missing_task_writes_nothing_including_the_parent_edge(server, commits):
    """The early return has to precede every step, extras included — otherwise
    updating a nonexistent task still leaves a dangling ParentOf edge."""
    parent = server.task_create(title="parent", description="p", repo=REPO)
    commits.clear()

    assert server.task_update(slug="tk-does-not-exist", parent=parent["slug"]) is None
    assert commits == [], f"a missing task must write nothing: {commits}"


@requires_omnigraph
def test_code_branch_tracking_survives_a_write_failure(server, tmp_path, monkeypatch):
    """Best-effort is preserved where it matters: `_track_code_branch` is the
    standalone path (task_claim's tail), and a store that refuses the write must
    not turn a won claim into a failed tool call."""
    from witan import server as srv

    base = _git_repo(tmp_path / "r")
    _git(base, "checkout", "-q", "-b", "feature/failing")
    monkeypatch.chdir(base)

    def boom(*_args, **_kwargs):
        raise RuntimeError("omnigraph mutate failed")

    monkeypatch.setattr(srv.client, "change_many", boom)

    srv._track_code_branch(REPO, task_slug="tk-whatever")  # must not raise


@requires_omnigraph
def test_task_link_blocks_with_status_sync_is_one_commit(server, commits):
    """It used to be two: the `Blocks` edge, then a separate update flipping
    the blocked task's `blocked_by` and (here) its `status` to `blocked`."""
    blocker = server.task_create(title="blocker", description="b", repo=REPO)
    blocked = server.task_create(title="blocked", description="c", repo=REPO)
    commits.clear()

    server.task_link(blocker["slug"], blocked["slug"], kind="blocks")

    assert len(commits) == 1, f"expected one commit, got {commits}"
    updated = server.task_get(blocked["slug"])
    assert updated["blocked_by"] == [blocker["slug"]]
    assert updated["status"] == "blocked"


@requires_omnigraph
def test_task_link_blocks_already_recorded_is_still_one_commit(server, commits):
    """The edge-only path — the blocker is already in `blocked_by`, so there
    is nothing left to sync — must not regress to zero commits or silently
    drop the (redundant, but requested) edge write."""
    blocker = server.task_create(title="blocker2", description="b", repo=REPO)
    blocked = server.task_create(title="blocked2", description="c", repo=REPO)
    server.task_link(blocker["slug"], blocked["slug"], kind="blocks")
    commits.clear()

    server.task_link(blocker["slug"], blocked["slug"], kind="blocks")

    assert commits == ["link_blocks"], f"expected the bare edge write: {commits}"


@requires_omnigraph
def test_task_link_parent_is_one_commit(server, commits):
    """Mirrors `task_update(parent=…)`'s own batching — `task_link` is the
    other way to set the same edge and must cost the same one commit."""
    from witan import server as srv

    parent = server.task_create(title="parent-link", description="p", repo=REPO)
    child = server.task_create(title="child-link", description="c", repo=REPO)
    commits.clear()

    server.task_link(parent["slug"], child["slug"], kind="parent")

    assert len(commits) == 1, f"expected one commit, got {commits}"
    row = srv.client.read("read.gq", "get_task", {"slug": child["slug"]})[0]
    assert row["parent_slug"] == parent["slug"]


@requires_omnigraph
def test_workflow_project_block_with_sync_is_one_commit(server, commits):
    """The `ProjectBlocks` edge and the blocked project's denormalized
    `blocked_by` used to be two separate commits."""
    blocker = server.workflow_project_create(title="blocker-proj", description="d")
    blocked = server.workflow_project_create(title="blocked-proj", description="d")
    commits.clear()

    server.workflow_project_block(blocker["slug"], blocked["slug"])

    assert len(commits) == 1, f"expected one commit, got {commits}"
    updated = server.workflow_project_get(blocked["slug"])
    assert updated["blocked_by"] == [blocker["slug"]]


@requires_omnigraph
def test_workflow_project_block_already_recorded_is_still_one_commit(server, commits):
    """Mirrors the task-link case: once `blocker` is already in `blocked_by`,
    a repeat call has nothing to sync and must be the bare edge write."""
    blocker = server.workflow_project_create(title="blocker-proj2", description="d")
    blocked = server.workflow_project_create(title="blocked-proj2", description="d")
    server.workflow_project_block(blocker["slug"], blocked["slug"])
    commits.clear()

    server.workflow_project_block(blocker["slug"], blocked["slug"])

    assert commits == ["link_project_blocks"], f"expected the bare edge: {commits}"


@requires_omnigraph
def test_trace_mine_trailing_writes_are_one_commit(server, commits):
    """It used to be N+1: an `Informed` edge per mined memory, one at a time,
    plus a separate trailing update to the trace's own annotation fields. The
    per-memory `_store_memory` commits themselves stay separate — each is a
    genuinely distinct row — only the WRAP-UP after them collapses to one."""
    proj = server.workflow_project_create(title="mine batched", description="d")
    done = server.workflow_project_complete(proj["slug"], outcome="shipped it")
    commits.clear()

    created = server.workflow_trace_mine(
        done["trace_slug"],
        patterns=[{"title": "a pattern", "content": "do X because Y"}],
        lessons=[{"title": "a lesson", "content": "watch out for Z"}],
    )

    # 2 commits for the two mined memories (unavoidable, one row each) + 1
    # batched commit for both `Informed` edges and the trace annotation —
    # NOT the 4 it used to be (2 memories + 2 edges + 1 annotate).
    assert len(commits) == 3, f"expected 3 commits, got {commits}"
    expected_batch = "link_informed+link_informed+update_workflow_trace_annotations"
    assert commits[-1] == expected_batch, (
        f"expected the trailing batch, got {commits[-1]!r}"
    )
    assert len(created["created_patterns"]) == 1
    assert len(created["created_lessons"]) == 1


@requires_omnigraph
def test_step_builders_are_pure_until_the_caller_commits(server, tmp_path, monkeypatch):
    """The property the refactor rests on: building steps writes nothing.

    If a builder ever issued its own write again, the caller would commit it a
    second time — and the batching would silently stop being a batch.
    """
    from witan import server as srv

    base = _git_repo(tmp_path / "r")
    _git(base, "checkout", "-q", "-b", "feature/pure")
    monkeypatch.chdir(base)

    def fail(*_args, **_kwargs):
        raise AssertionError("a step builder must not write")

    monkeypatch.setattr(srv.client, "change", fail)
    monkeypatch.setattr(srv.client, "change_many", fail)

    steps = srv._code_branch_steps(REPO, project_slug="wp-anything")

    assert steps, "expected an upsert step and a ForProject edge step"
    slug = srv._code_branch_slug(REPO, "feature/pure")
    assert srv.client.read("read.gq", "get_code_branch", {"slug": slug}) == []
