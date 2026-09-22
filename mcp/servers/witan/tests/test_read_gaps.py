"""The reads the Witan UI views need, which the tools did not give.

Six gaps from the UI spec (`docs/internals/design/witan-ui-spec.md` §3.1-3.4,
§3.7, §3.8). They are one change because they share a rule: each is fixed in
the TOOL, so an agent and the page read the same thing, rather than in a
browser-side workaround that would make the two disagree.

The three that are silent failures rather than missing fields are worth naming,
because a passing assertion elsewhere never revealed them:

  * an unscoped `task_list` returned the 50 most recently updated tasks and
    said nothing about the rest, so "all repos" on the board was a lie with no
    tell;
  * `workflow_project_status` reported `counts.ready = 100` for a project with
    any larger number of ready tasks;
  * every `in_progress` row looked identical whether its claim was minutes old
    or a crashed agent's from last week.
"""

from datetime import datetime, timedelta, timezone

import pytest
from witan import readiness

from .conftest import requires_omnigraph

# `server` sets WITAN_REPO, so an unpassed `repo` resolves to it and takes the
# repo-scoped branch. `repo=""` is the documented spelling for "every repo",
# and it is the only way to reach the branch that was capped at 50.
ALL_REPOS = ""


def _iso(seconds_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


# ── §3.1 `task_list` truncated unscoped reads at 50 rows ───────────


@requires_omnigraph
def test_unscoped_list_still_stops_at_fifty_without_a_limit(server):
    """The behaviour every existing caller has; `limit` must not change it."""
    for i in range(51):
        server.task_create(title=f"task {i}", description="x")

    assert len(server.task_list(repo=ALL_REPOS)) == 50


@requires_omnigraph
def test_an_explicit_limit_reads_past_the_fifty_row_cap(server):
    """The defect: 51 tasks exist and no argument could see the 51st.

    The cap is a literal in `list_all_tasks`, so this is not a slice the
    server was choosing — a limit has to select the uncapped query.
    """
    for i in range(51):
        server.task_create(title=f"task {i}", description="x")

    assert len(server.task_list(repo=ALL_REPOS, limit=51)) == 51


@requires_omnigraph
def test_an_explicit_limit_reads_past_the_cap_with_a_status_filter(server):
    """The other capped query. `list_tasks_by_status` had the same literal 50."""
    for i in range(51):
        server.task_create(title=f"task {i}", description="x")

    rows = server.task_list(repo=ALL_REPOS, status="open", limit=51)

    assert len(rows) == 51
    assert {r["status"] for r in rows} == {"open"}


@requires_omnigraph
def test_a_limit_also_bounds_a_scoped_read(server):
    """Scoped reads are uncapped today, so a limit has to apply there too."""
    project = server.workflow_project_create(title="p", description="d")
    for i in range(5):
        server.task_create(
            title=f"task {i}", description="x", project_slug=project["slug"]
        )

    assert len(server.task_list(project_slug=project["slug"], limit=2)) == 2


@requires_omnigraph
def test_a_scoped_read_stays_uncapped_when_no_limit_is_given(server):
    """`limit` is nullable precisely so this case keeps its old behaviour."""
    project = server.workflow_project_create(title="p", description="d")
    for i in range(5):
        server.task_create(
            title=f"task {i}", description="x", project_slug=project["slug"]
        )

    assert len(server.task_list(project_slug=project["slug"])) == 5


@requires_omnigraph
def test_a_limit_bounds_the_repo_detected_read(server):
    """The DEFAULT branch: `repo` is auto-detected, so this is what an in-repo
    caller gets. It concatenates repo-scoped rows with unscoped ones rather
    than merging them on `updated_at`, so the slice keeps this repo's tasks.
    """
    for i in range(5):
        server.task_create(title=f"task {i}", description="x")

    rows = server.task_list(limit=2)

    assert len(rows) == 2
    assert {r["repo"] for r in rows} == {"https://github.com/test/repo"}


@pytest.mark.parametrize("limit", [0, -1, 10001])
@requires_omnigraph
def test_a_limit_outside_the_range_is_refused(server, limit):
    """Loudly, not by clamping. 10,000 is what the uncapped queries can return,
    so a larger limit would otherwise come back short with nothing saying so."""
    with pytest.raises(ValueError, match="between 1 and 10000"):
        server.task_list(repo=ALL_REPOS, limit=limit)


# ── §3.2 list rows carried no created_at or closed_at ──────────────


@requires_omnigraph
def test_list_rows_carry_both_timestamps(server):
    """Only `get_task` returned these, so the Gantt would cost one read a task."""
    open_task = server.task_create(title="open one", description="x")
    done = server.task_create(title="closed one", description="x")
    server.task_close(done["slug"], resolution="done")

    rows = {r["slug"]: r for r in server.task_list()}

    assert rows[open_task["slug"]]["created_at"]
    assert rows[open_task["slug"]]["closed_at"] is None
    assert rows[done["slug"]]["closed_at"]


# ── §3.3 task detail was missing its edges ─────────────────────────


@requires_omnigraph
def test_task_get_returns_the_tasks_this_one_blocks(server):
    """The inverse of `blocked_by`, which the node field cannot express.

    One write, not two: `task_create(blocked_by=...)` already writes the
    `Blocks` edge (server.py:6086). Calling `task_link` as well would leave the
    test unable to tell which path produced the row.
    """
    blocker = server.task_create(title="blocker", description="x")
    blocked = server.task_create(
        title="blocked", description="x", blocked_by=[blocker["slug"]]
    )

    assert server.task_get(blocker["slug"])["blocks"] == [blocked["slug"]]


@requires_omnigraph
def test_task_get_blocks_is_not_duplicated_by_an_explicit_link(server):
    """`task_link` over an edge `task_create` already wrote is one row, not two."""
    blocker = server.task_create(title="blocker", description="x")
    blocked = server.task_create(
        title="blocked", description="x", blocked_by=[blocker["slug"]]
    )
    server.task_link(blocker["slug"], blocked["slug"], "blocks")

    assert server.task_get(blocker["slug"])["blocks"] == [blocked["slug"]]


@requires_omnigraph
def test_task_get_returns_its_children(server):
    """The drill-down's epic level."""
    epic = server.task_create(title="epic", description="x", type="epic")
    child = server.task_create(title="child", description="x", parent=epic["slug"])

    children = server.task_get(epic["slug"])["children"]

    assert [c["slug"] for c in children] == [child["slug"]]
    assert children[0]["title"] == "child"
    assert children[0]["status"] == "open"


@requires_omnigraph
def test_task_get_returns_the_branches_working_it(server):
    """`task_code_branches` was declared in read.gq and called by nothing."""
    task = server.task_create(title="t", description="x")
    server.task_claim(task["slug"], assignee="agentA", branch="feature/thing")

    branches = server.task_get(task["slug"])["branches"]

    assert [b["branch"] for b in branches] == ["feature/thing"]


@requires_omnigraph
def test_a_task_with_no_edges_gets_empty_lists(server):
    """Absent edges are `[]`, not missing keys — a view should not branch."""
    node = server.task_get(server.task_create(title="t", description="x")["slug"])

    assert node["blocks"] == []
    assert node["children"] == []
    assert node["branches"] == []


@requires_omnigraph
def test_task_get_survives_a_store_with_no_codebranch_type(server, monkeypatch):
    """★ THE REASON `branches` IS THE ONE GUARDED EDGE.

    `CodeBranch` was added after the first stores were provisioned, and
    `_ensure_graph` re-applies `schema.pg` only to a LOCAL store, so a deployed
    graph from before it answers "unknown node type". `context.py:353-368` has
    isolated the identical read since that type landed. Letting it propagate
    here would break `task_get` outright on such a store, taking the whole task
    surface down to add one field to it.

    `blocks` and `children` are deliberately NOT guarded: they touch only
    `Task` and `Blocks`, which any store answering `get_task` already has.
    """
    from witan import server as srv

    task = server.task_create(title="t", description="x")
    real_read = srv.client.read

    def read(file: str, name: str, params: dict):
        if name == "task_code_branches":
            msg = "unknown node/edge type: CodeBranch"
            raise RuntimeError(msg)
        return real_read(file, name, params)

    monkeypatch.setattr(srv.client, "read", read)

    node = server.task_get(task["slug"])

    assert node["branches"] == []
    assert node["slug"] == task["slug"]


@requires_omnigraph
def test_task_get_still_raises_on_a_real_branch_read_failure(server, monkeypatch):
    """Only the missing-type error is swallowed; anything else propagates."""
    from witan import server as srv

    task = server.task_create(title="t", description="x")
    real_read = srv.client.read

    def read(file: str, name: str, params: dict):
        if name == "task_code_branches":
            msg = "connection reset by peer"
            raise RuntimeError(msg)
        return real_read(file, name, params)

    monkeypatch.setattr(srv.client, "read", read)

    with pytest.raises(RuntimeError, match="connection reset"):
        server.task_get(task["slug"])


# ── §3.4 stale claims were invisible on a task row ─────────────────


@requires_omnigraph
def test_a_live_claim_reports_its_lease_as_unexpired(server):
    task = server.task_create(title="t", description="x")
    server.task_claim(task["slug"], assignee="agentA")

    assert server.task_get(task["slug"])["lease_expired"] is False


@requires_omnigraph
def test_a_lapsed_claim_is_marked_on_every_row_shape(server, monkeypatch):
    """`task_get`, `task_list` and `task_ready` must agree, since the board
    reads one and the detail pane the other."""
    monkeypatch.setattr(readiness, "CLAIM_LEASE_SECONDS", -1)
    task = server.task_create(title="t", description="x")
    server.task_claim(task["slug"], assignee="agentA")

    listed = next(r for r in server.task_list() if r["slug"] == task["slug"])
    ready = next(r for r in server.task_ready() if r["slug"] == task["slug"])

    assert server.task_get(task["slug"])["lease_expired"] is True
    assert listed["lease_expired"] is True
    assert ready["lease_expired"] is True


@requires_omnigraph
def test_only_in_progress_rows_carry_the_flag(server):
    """A `false` on an open or closed task would read as "claim still live"."""
    open_task = server.task_create(title="open", description="x")
    done = server.task_create(title="done", description="x")
    server.task_close(done["slug"], resolution="done")

    rows = {r["slug"]: r for r in server.task_list()}

    assert "lease_expired" not in rows[open_task["slug"]]
    assert "lease_expired" not in rows[done["slug"]]


@requires_omnigraph
def test_a_lapsed_claim_on_a_blocked_task_is_still_marked(server, monkeypatch):
    """THE CASE THE BOARD CANNOT INFER, and the reason this is a field.

    A UI could otherwise read "in_progress but listed by task_ready" as an
    expired lease. That misses an abandoned task that also has an open
    blocker: `readiness.is_ready` requires every blocker closed, so it never
    appears in Ready however stale its claim — which is the abandoned work
    most worth seeing, because something else is waiting on it.
    """
    monkeypatch.setattr(readiness, "CLAIM_LEASE_SECONDS", -1)
    blocker = server.task_create(title="blocker", description="x")
    abandoned = server.task_create(title="abandoned", description="x")
    # Claimed first, blocked after — `task_claim` refuses a blocked task, and
    # this is how the state arises anyway: work starts, then something it
    # depends on is discovered. `task_link` leaves an `in_progress` status
    # alone, flipping only an `open` one to `blocked`.
    server.task_claim(abandoned["slug"], assignee="agentA")
    server.task_link(blocker["slug"], abandoned["slug"], "blocks")

    listed = next(r for r in server.task_list() if r["slug"] == abandoned["slug"])
    assert listed["status"] == "in_progress"
    assert listed["blocked_by"] == [blocker["slug"]]

    assert abandoned["slug"] not in [r["slug"] for r in server.task_ready()]
    assert listed["lease_expired"] is True


@requires_omnigraph
def test_task_update_to_in_progress_stamps_a_live_lease(server):
    """`task_update(status="in_progress")` stamps `claimed_at` (server.py:6662),
    so this row has a real lease rather than reaching the fallback below."""
    task = server.task_create(title="t", description="x")
    server.task_update(task["slug"], status="in_progress")

    node = server.task_get(task["slug"])

    assert node["claimed_at"]
    assert node["lease_expired"] is False


# The legacy shape cannot be built through the tools: every path that sets
# `in_progress` stamps `claimed_at`. So the fallback is exercised against the
# helper directly, which is the only honest way to reach a row that predates
# that stamp without hand-writing one into the store.


def test_the_flag_falls_back_to_updated_at_when_a_row_has_no_claim():
    """A legacy `in_progress` row is held until its LAST WRITE ages out.

    Reading "no claimed_at" as "no lease, therefore free" would make such a
    row pickable from the instant it started, forever, which is the
    double-work bug the lease exists to prevent.
    """
    from witan import server as srv

    fresh = srv._with_lease_flag(
        {"status": "in_progress", "claimed_at": None, "updated_at": _iso(60)}
    )
    stale = srv._with_lease_flag(
        {
            "status": "in_progress",
            "claimed_at": None,
            "updated_at": _iso(readiness.CLAIM_LEASE_SECONDS + 60),
        }
    )

    assert fresh["lease_expired"] is False
    assert stale["lease_expired"] is True


# ── §3.7 session history was read whole ────────────────────────────


@requires_omnigraph
def test_since_drops_sessions_that_ended_before_the_window(server):
    project = server.workflow_project_create(title="p", description="d")
    old = server.workflow_session_start(project["slug"], "sid-old", "implementation")
    server.workflow_session_end(old["session_slug"], summary="old work")

    rows = server.workflow_session_list(since=_iso(-3600))

    assert old["session_slug"] not in [r["slug"] for r in rows]


@requires_omnigraph
def test_since_keeps_a_session_that_ended_inside_the_window(server):
    project = server.workflow_project_create(title="p", description="d")
    recent = server.workflow_session_start(project["slug"], "sid-new", "implementation")
    server.workflow_session_end(recent["session_slug"], summary="recent work")

    rows = server.workflow_session_list(since=_iso(3600))

    assert recent["session_slug"] in [r["slug"] for r in rows]


@requires_omnigraph
def test_since_never_drops_an_open_session(server):
    """An open session is running NOW. A window that excluded it would hide
    exactly the sessions a live timeline exists to show."""
    project = server.workflow_project_create(title="p", description="d")
    live = server.workflow_session_start(project["slug"], "sid-live", "implementation")

    rows = server.workflow_session_list(since=_iso(-86400))

    assert live["session_slug"] in [r["slug"] for r in rows]


@requires_omnigraph
def test_an_unparseable_since_is_refused(server):
    """★ NOT tolerated, unlike a malformed stored `ended_at`.

    The comparison runs per row, so swallowing a bad `since` there would make
    every row compare true and `since="last week"` would quietly return every
    session ever recorded. That is the same unsignalled no-op this parameter
    exists to remove, so it raises instead.
    """
    with pytest.raises(ValueError, match="since must be an ISO timestamp"):
        server.workflow_session_list(since="last week")


@requires_omnigraph
def test_an_empty_since_is_refused_rather_than_ignored(server):
    """★ Supplied-and-empty is a bad value, not an omission.

    `if since:` skipped the filter on `""`, so a caller that built the
    argument from an empty form field got every session ever recorded and no
    indication that the window had been dropped. Omitted still means omitted.
    """
    with pytest.raises(ValueError, match="since must be an ISO timestamp"):
        server.workflow_session_list(since="")


@requires_omnigraph
def test_a_naive_since_is_read_as_utc(server):
    """A caller passing a tz-less timestamp gets an answer, not a TypeError.

    Comparing a naive datetime against the aware ones the store writes raises,
    and that would surface as a crash on a plausible input.
    """
    project = server.workflow_project_create(title="p", description="d")
    live = server.workflow_session_start(project["slug"], "sid-naive", "implementation")
    server.workflow_session_end(live["session_slug"], summary="done")

    naive = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None)

    rows = server.workflow_session_list(since=naive.isoformat())

    assert live["session_slug"] in [r["slug"] for r in rows]


@requires_omnigraph
def test_without_since_every_session_is_returned(server):
    """The existing callers' behaviour, unchanged."""
    project = server.workflow_project_create(title="p", description="d")
    ended = server.workflow_session_start(project["slug"], "sid-1", "implementation")
    server.workflow_session_end(ended["session_slug"], summary="done")

    assert ended["session_slug"] in [r["slug"] for r in server.workflow_session_list()]


# ── §3.8 the project rollup capped and miscounted ready work ───────


@requires_omnigraph
def test_the_ready_count_is_exact_while_the_list_stays_capped(server, monkeypatch):
    """The cap is patched down rather than creating 101 tasks: what is under
    test is that the count is taken BEFORE the truncation, not the number 100.
    """
    from witan import server as srv

    monkeypatch.setattr(srv, "_PROJECT_READY_CAP", 3)
    project = server.workflow_project_create(title="p", description="d")
    for i in range(5):
        server.task_create(
            title=f"task {i}", description="x", project_slug=project["slug"]
        )

    status = server.workflow_project_status(project["slug"])

    assert status["counts"]["ready"] == 5
    assert len(status["ready_tasks"]) == 3
    assert status["ready_truncated"] is True


@requires_omnigraph
def test_the_ready_count_has_no_ceiling_of_its_own(server, monkeypatch):
    """The count is bounded by the project's task count, not by a constant.

    Passing a fixed ceiling to `task_ready` would put the same silent cap
    back, just further out. Ready tasks are a subset of the project's tasks,
    so slicing at that size cannot truncate. Checked by shrinking the CAP,
    which is what `ready_tasks` is limited by, and asserting the COUNT ignores
    it.
    """
    from witan import server as srv

    monkeypatch.setattr(srv, "_PROJECT_READY_CAP", 1)
    monkeypatch.setattr(srv, "_MAX_TASK_LIMIT", 2)
    project = server.workflow_project_create(title="p", description="d")
    for i in range(5):
        server.task_create(
            title=f"task {i}", description="x", project_slug=project["slug"]
        )

    status = server.workflow_project_status(project["slug"])

    # 5, not 2: a `_MAX_TASK_LIMIT`-shaped ceiling would have clamped it.
    assert status["counts"]["ready"] == 5
    assert len(status["ready_tasks"]) == 1
    assert status["ready_truncated"] is True


@requires_omnigraph
def test_an_untruncated_rollup_says_so(server, monkeypatch):
    from witan import server as srv

    monkeypatch.setattr(srv, "_PROJECT_READY_CAP", 3)
    project = server.workflow_project_create(title="p", description="d")
    for i in range(2):
        server.task_create(
            title=f"task {i}", description="x", project_slug=project["slug"]
        )

    status = server.workflow_project_status(project["slug"])

    assert status["counts"]["ready"] == 2
    assert len(status["ready_tasks"]) == 2
    assert status["ready_truncated"] is False
