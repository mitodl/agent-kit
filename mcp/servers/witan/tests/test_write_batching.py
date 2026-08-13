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
