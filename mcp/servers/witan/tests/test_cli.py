"""End-to-end tests for CLI create subcommands and graph visualization."""

from .conftest import requires_omnigraph


def _patch_server(monkeypatch, srv):
    """Redirect CLI server calls to a test server and capture console output."""
    from witan.cli import _common

    monkeypatch.setattr(_common, "_server", srv)
    printed = []
    monkeypatch.setattr(
        _common.console, "print", lambda *a, **kw: printed.append(str(a[0]))
    )
    return printed


@requires_omnigraph
def test_project_create_minimal(server, monkeypatch):
    """project_create with just title/description creates a project in discovery phase."""
    from witan.cli.projects import project_create

    printed = _patch_server(monkeypatch, server)
    project_create(title="My Feature", description="build it")

    combined = "\n".join(printed)
    assert "wp-" in combined
    assert "discovery" in combined


@requires_omnigraph
def test_project_create_with_phase_and_repo(server, monkeypatch):
    """project_create accepts phase and repo overrides."""
    from witan import server as srv
    from witan.cli._common import _fn
    from witan.cli.projects import project_create

    printed = _patch_server(monkeypatch, server)
    project_create(
        title="Spec Phase Project",
        description="full desc",
        phase="spec",
        repo="https://github.com/test/extra",
        github_issue="https://github.com/test/repo/issues/1",
        tags=["alpha", "beta"],
    )

    combined = "\n".join(printed)
    assert "wp-" in combined
    assert "spec" in combined

    rows = _fn(srv.workflow_project_list)(repo="", status=None)
    assert any("spec" == r.get("phase") for r in rows)


@requires_omnigraph
def test_task_create_minimal(server, monkeypatch):
    """task_create_cmd with just title/description produces an open task."""
    from witan.cli.tasks import task_create_cmd

    printed = _patch_server(monkeypatch, server)
    task_create_cmd(title="Do the thing", description="details here")

    combined = "\n".join(printed)
    assert "tk-" in combined
    assert "open" in combined


@requires_omnigraph
def test_task_create_with_project_and_blocker(server, monkeypatch):
    """task_create_cmd links to a project and a blocker correctly."""
    from witan import server as srv
    from witan.cli._common import _fn
    from witan.cli.tasks import task_create_cmd

    printed = _patch_server(monkeypatch, server)

    blocker = _fn(srv.task_create)(title="blocker", description="x")
    proj = _fn(srv.workflow_project_create)(title="P", description="d")

    task_create_cmd(
        title="Dependent task",
        description="needs the blocker",
        type="feature",
        priority="p1",
        project=proj["slug"],
        blocked_by=[blocker["slug"]],
        external_uri="https://github.com/test/repo/issues/5",
    )

    combined = "\n".join(printed)
    assert "tk-" in combined
    assert "blocked" in combined


@requires_omnigraph
def test_task_create_with_parent(server, monkeypatch):
    """task_create_cmd accepts parent and tags and produces an open child task."""
    from witan import server as srv
    from witan.cli._common import _fn
    from witan.cli.tasks import task_create_cmd

    printed = _patch_server(monkeypatch, server)

    epic = _fn(srv.task_create)(title="parent epic", description="x", type="epic")

    task_create_cmd(
        title="Child task",
        description="sub work",
        type="chore",
        priority="p3",
        repo="https://github.com/test/repo",
        parent=epic["slug"],
        tags=["cleanup"],
    )

    combined = "\n".join(printed)
    assert "tk-" in combined
    assert "open" in combined


@requires_omnigraph
def test_project_advance_cli(server, monkeypatch):
    """project advance moves a project to a new phase."""
    from witan import server as srv
    from witan.cli._common import _fn
    from witan.cli.projects import project_advance

    printed = _patch_server(monkeypatch, server)
    proj = _fn(srv.workflow_project_create)(title="P", description="d", phase="spec")

    project_advance(proj["slug"], phase="implementation")

    combined = "\n".join(printed)
    assert "Advanced" in combined
    assert "implementation" in combined
    fresh = _fn(srv.workflow_project_get)(proj["slug"])
    assert fresh["phase"] == "implementation"


@requires_omnigraph
def test_project_advance_backward_shows_advisory(server, monkeypatch):
    """A backward transition still commits from the CLI but surfaces the advisory."""
    from witan import server as srv
    from witan.cli._common import _fn
    from witan.cli.projects import project_advance

    printed = _patch_server(monkeypatch, server)
    proj = _fn(srv.workflow_project_create)(
        title="P", description="d", phase="implementation"
    )

    project_advance(proj["slug"], phase="spec")

    combined = "\n".join(printed)
    assert "note" in combined.lower() or "not advanced" in combined.lower()


@requires_omnigraph
def test_project_complete_cli(server, monkeypatch):
    """project complete seals the project and reports a trace slug."""
    from witan import server as srv
    from witan.cli._common import _fn
    from witan.cli.projects import project_complete

    printed = _patch_server(monkeypatch, server)
    proj = _fn(srv.workflow_project_create)(title="Done", description="d")

    project_complete(proj["slug"], outcome="Shipped the whole thing end to end.")

    combined = "\n".join(printed)
    assert "Completed" in combined
    assert f"wt-{proj['slug']}" in combined
    fresh = _fn(srv.workflow_project_get)(proj["slug"])
    assert fresh["status"] == "completed"


@requires_omnigraph
def test_project_block_and_unblock_cli(server, monkeypatch):
    """project block/unblock manage a project dependency."""
    from witan import server as srv
    from witan.cli._common import _fn
    from witan.cli.projects import project_block, project_unblock

    printed = _patch_server(monkeypatch, server)
    a = _fn(srv.workflow_project_create)(title="A", description="d")
    b = _fn(srv.workflow_project_create)(title="B", description="d")

    project_block(a["slug"], b["slug"])
    assert a["slug"] in (
        _fn(srv.workflow_project_get)(b["slug"]).get("blocked_by") or []
    )

    project_unblock(a["slug"], b["slug"])
    assert a["slug"] not in (
        _fn(srv.workflow_project_get)(b["slug"]).get("blocked_by") or []
    )

    combined = "\n".join(printed)
    assert "Blocked" in combined
    assert "Unblocked" in combined


@requires_omnigraph
def test_project_tasks_detail_cli(server, monkeypatch):
    """project tasks --detail lists tasks and their blocker/dependent edges."""
    from witan import server as srv
    from witan.cli._common import _fn
    from witan.cli.projects import project_tasks

    printed = _patch_server(monkeypatch, server)
    proj = _fn(srv.workflow_project_create)(title="Dep Project", description="d")
    blocker = _fn(srv.task_create)(
        title="the blocker", description="d", project_slug=proj["slug"]
    )
    blocked = _fn(srv.task_create)(
        title="the blocked",
        description="d",
        project_slug=proj["slug"],
        blocked_by=[blocker["slug"]],
    )

    project_tasks(proj["slug"], detail=True)

    combined = "\n".join(printed)
    assert blocker["slug"] in combined
    assert blocked["slug"] in combined
    assert "Dependencies" in combined
    # the blocked task shows it is blocked by the blocker; the blocker shows it
    # blocks the blocked task
    assert "blocked by" in combined
    assert "blocks" in combined


@requires_omnigraph
def test_project_tasks_no_detail_omits_dependency_section(server, monkeypatch):
    """Without --detail, only the task table prints (no Dependencies section)."""
    from witan import server as srv
    from witan.cli._common import _fn
    from witan.cli.projects import project_tasks

    printed = _patch_server(monkeypatch, server)
    proj = _fn(srv.workflow_project_create)(title="Flat Project", description="d")
    _fn(srv.task_create)(title="lone task", description="d", project_slug=proj["slug"])

    project_tasks(proj["slug"])

    combined = "\n".join(printed)
    assert "lone task" in combined
    assert "Dependencies" not in combined


@requires_omnigraph
def test_task_close_cli(server, monkeypatch):
    """task close sets a task closed with a resolution."""
    from witan import server as srv
    from witan.cli._common import _fn
    from witan.cli.tasks import task_close_cmd

    printed = _patch_server(monkeypatch, server)
    t = _fn(srv.task_create)(title="t", description="d")

    task_close_cmd(t["slug"], resolution="did it")

    combined = "\n".join(printed)
    assert "Closed" in combined
    assert _fn(srv.task_get)(t["slug"])["status"] == "closed"


@requires_omnigraph
def test_task_claim_and_release_cli(server, monkeypatch):
    """task claim then release round-trips ownership."""
    from witan import server as srv
    from witan.cli._common import _fn
    from witan.cli.tasks import task_claim_cmd, task_release_cmd

    printed = _patch_server(monkeypatch, server)
    t = _fn(srv.task_create)(title="t", description="d")

    task_claim_cmd(t["slug"], assignee="alice")
    assert _fn(srv.task_get)(t["slug"])["assignee"] == "alice"

    task_release_cmd(t["slug"], assignee="alice")
    assert _fn(srv.task_get)(t["slug"])["status"] == "open"

    combined = "\n".join(printed)
    assert "Claimed" in combined
    assert "Released" in combined


@requires_omnigraph
def test_task_update_cli(server, monkeypatch):
    """task update changes mutable fields."""
    from witan import server as srv
    from witan.cli._common import _fn
    from witan.cli.tasks import task_update_cmd

    printed = _patch_server(monkeypatch, server)
    t = _fn(srv.task_create)(title="t", description="d", priority="p3")

    task_update_cmd(t["slug"], priority="p0", status="in_progress")

    combined = "\n".join(printed)
    assert "Updated" in combined
    fresh = _fn(srv.task_get)(t["slug"])
    assert fresh["priority"] == "p0"
    assert fresh["status"] == "in_progress"


@requires_omnigraph
def test_task_link_cli(server, monkeypatch):
    """task link blocks establishes a blocked_by edge."""
    from witan import server as srv
    from witan.cli._common import _fn
    from witan.cli.tasks import task_link_cmd

    printed = _patch_server(monkeypatch, server)
    blocker = _fn(srv.task_create)(title="blocker", description="d")
    blocked = _fn(srv.task_create)(title="blocked", description="d")

    task_link_cmd(blocker["slug"], blocked["slug"], kind="blocks")

    combined = "\n".join(printed)
    assert "Linked" in combined
    assert blocker["slug"] in (
        _fn(srv.task_get)(blocked["slug"]).get("blocked_by") or []
    )


@requires_omnigraph
def test_session_start_end_list_cli(server, monkeypatch):
    """session start/end/list drive and inspect a project's sessions."""
    from witan import server as srv
    from witan.cli._common import _fn
    from witan.cli.session import session_end, session_list, session_start

    printed = _patch_server(monkeypatch, server)
    proj = _fn(srv.workflow_project_create)(title="S", description="d")

    session_start(proj["slug"], phase="implementation", session_id="cli-test-1")
    started = "\n".join(printed)
    assert "Started session" in started
    # The ws- slug is echoed; pull it back out to end the session.
    import re

    m = re.search(r"ws-[\w-]+", started)
    assert m
    ws_slug = m.group(0)

    session_end(ws_slug, summary="did the work, more to do")
    session_list(proj["slug"])

    combined = "\n".join(printed)
    assert "Ended session" in combined
    assert ws_slug in combined
    assert "did the work" in combined


def _table_column(table, header):
    """Return a rich Table column's cell values by header name."""
    for col in table.columns:
        if col.header == header:
            return list(col._cells)
    return []


@requires_omnigraph
def test_tasks_elides_closed_by_default(server, monkeypatch):
    """`witan tasks` hides closed tasks unless --status is given."""
    from witan.cli import _common
    from witan.cli._common import _fn
    from witan.cli.tasks import tasks

    monkeypatch.setattr(_common, "_server", server)
    captured = []
    monkeypatch.setattr(_common.console, "print", lambda *a, **k: captured.append(a[0]))

    live = _fn(server.task_create)(title="live work", description="d")
    done = _fn(server.task_create)(title="finished work", description="d")
    _fn(server.task_close)(done["slug"])

    tasks(all_repos=True)
    table = next(c for c in captured if hasattr(c, "columns"))
    slugs = _table_column(table, "slug")
    assert live["slug"] in slugs
    assert done["slug"] not in slugs

    # --status closed surfaces the closed one (and only it)
    captured.clear()
    tasks(all_repos=True, status="closed")
    table = next(c for c in captured if hasattr(c, "columns"))
    slugs = _table_column(table, "slug")
    assert done["slug"] in slugs
    assert live["slug"] not in slugs


@requires_omnigraph
def test_graph_command_rich_output(server, monkeypatch):
    """witan graph prints projects and tasks without requiring HTML output."""
    from witan import server as srv
    from witan.cli._common import _fn
    from witan.cli.graph import graph

    printed = _patch_server(monkeypatch, server)

    proj = _fn(srv.workflow_project_create)(title="Viz Project", description="test")
    _fn(srv.task_create)(
        title="Task A",
        description="first task",
        project_slug=proj["slug"],
    )
    _fn(srv.task_create)(
        title="Task B",
        description="second task",
        project_slug=proj["slug"],
    )

    graph(all_repos=True, status=None)

    combined = "\n".join(printed)
    assert "Workflow graph" in combined
    assert "1 projects" in combined
    assert "2 tasks" in combined


@requires_omnigraph
def test_graph_command_html_output(server, monkeypatch, tmp_path):
    """witan graph --html writes a vis-network HTML file."""
    from witan import server as srv
    from witan.cli._common import _fn
    from witan.cli.graph import graph

    _patch_server(monkeypatch, server)

    proj = _fn(srv.workflow_project_create)(title="HTML Graph", description="html test")
    blocker = _fn(srv.task_create)(title="Blocker", description="b")
    blocked = _fn(srv.task_create)(
        title="Blocked",
        description="b2",
        project_slug=proj["slug"],
        blocked_by=[blocker["slug"]],
    )

    out = tmp_path / "graph.html"
    graph(all_repos=True, status=None, html=out)

    html = out.read_text(encoding="utf-8")
    assert "vis-network" in html
    assert proj["slug"] in html
    assert blocker["slug"] in html
    assert blocked["slug"] in html
    assert '"group": "project"' in html
    assert '"group": "task"' in html


class _FakeMCP:
    """Records run()/mount() calls so serve() dispatch can be asserted."""

    def __init__(self):
        self.run_calls = []

    def mount(self, *a, **kw):  # pragma: no cover - only when witan-code present
        pass

    def run(self, *a, **kw):
        self.run_calls.append(kw)


def _patch_mcp(monkeypatch):
    from witan import server as srv

    fake = _FakeMCP()
    monkeypatch.setattr(srv, "mcp", fake)
    return fake


def test_serve_defaults_to_stdio(monkeypatch):
    from witan.cli import serve

    fake = _patch_mcp(monkeypatch)
    serve()
    assert fake.run_calls == [{}]


def test_serve_streamable_http_passes_transport_kwargs(monkeypatch):
    from witan.cli import serve

    from witan_core.observability.asgi import TraceContextASGIMiddleware

    fake = _patch_mcp(monkeypatch)
    serve(transport="streamable-http", host="0.0.0.0", port=9001, path="/witan")
    (call,) = fake.run_calls
    assert {
        "transport": call["transport"],
        "host": call["host"],
        "port": call["port"],
        "path": call["path"],
    } == {
        "transport": "streamable-http",
        "host": "0.0.0.0",
        "port": 9001,
        "path": "/witan",
    }
    # ★ The middleware is what joins witan's spans to ToolHive's trace, and it
    # only takes effect if it reaches `run()` — a deployed server whose spans
    # silently form their own trace looks healthy in every metric. Asserted
    # here rather than left to the witan-core unit tests, which cover the
    # middleware itself but not that anything installs it.
    (entry,) = call["middleware"]
    assert entry.cls is TraceContextASGIMiddleware


def test_serve_http_prepends_missing_leading_slash(monkeypatch):
    from witan.cli import serve

    fake = _patch_mcp(monkeypatch)
    serve(transport="http", path="mcp")
    assert fake.run_calls[0]["path"] == "/mcp"


def test_serve_overrides_fastmcps_two_second_shutdown_grace(monkeypatch):
    """★ FastMCP hardcodes `timeout_graceful_shutdown: 2`, which severs writes.

    A witan write has been measured at 27s under load. At the library default,
    every one in flight is dropped two seconds into a rollout, eviction or node
    drain — and a severed write is exactly the indeterminate outcome the caller
    cannot safely retry. The deployment's 150s `terminationGracePeriodSeconds`
    does not help on its own: it makes the kubelet willing to wait for time
    uvicorn declines to use.
    """
    from witan.cli import DEFAULT_SHUTDOWN_GRACE_SECONDS, serve

    fake = _patch_mcp(monkeypatch)
    serve(transport="streamable-http")
    (call,) = fake.run_calls
    assert call["uvicorn_config"]["timeout_graceful_shutdown"] == (
        DEFAULT_SHUTDOWN_GRACE_SECONDS
    )
    assert DEFAULT_SHUTDOWN_GRACE_SECONDS > 2, (  # noqa: PLR2004
        "the whole point is exceeding fastmcp's 2s default"
    )


def test_serve_shutdown_grace_is_tunable(monkeypatch):
    """So a deployment can match it to its own termination grace period."""
    from witan.cli import serve

    fake = _patch_mcp(monkeypatch)
    serve(transport="streamable-http", shutdown_grace_seconds=45.0)
    assert fake.run_calls[0]["uvicorn_config"]["timeout_graceful_shutdown"] == 45.0  # noqa: PLR2004


def test_stdio_serve_passes_no_uvicorn_config(monkeypatch):
    """stdio has no HTTP server to configure; passing one would be a TypeError
    waiting for the next fastmcp release to enforce it."""
    from witan.cli import serve

    fake = _patch_mcp(monkeypatch)
    serve()
    assert fake.run_calls == [{}]


# ── witan project update ──────────────────────────────────────────


@requires_omnigraph
def test_project_update_only_touches_what_is_passed(server, monkeypatch):
    from witan.cli.projects import project_create, project_update

    _patch_server(monkeypatch, server)
    project_create(
        title="Original", description="full desc", tags=["alpha"], phase="spec"
    )
    from witan.cli._common import _fn

    slug = _fn(server.workflow_project_list)(repo="")[0]["slug"]

    project_update(slug, title="Corrected")

    proj = _fn(server.workflow_project_get)(slug)
    assert proj["title"] == "Corrected"
    assert proj["description"] == "full desc"
    assert proj["tags"] == ["alpha"]
    assert proj["phase"] == "spec"


@requires_omnigraph
def test_project_update_adds_and_removes_repos(server, monkeypatch):
    """The headline case: a repo set guessed during discovery, before the work's
    real blast radius was known."""
    from witan.cli._common import _fn
    from witan.cli.projects import project_update

    _patch_server(monkeypatch, server)
    created = _fn(server.workflow_project_create)(
        title="blast radius", description="d", repos=["https://github.com/x/one"]
    )

    project_update(
        created["slug"],
        add_repo=["https://github.com/x/two"],
        remove_repo=["https://github.com/x/one"],
    )

    proj = _fn(server.workflow_project_get)(created["slug"])
    assert "https://github.com/x/two" in proj["repos"]
    assert "https://github.com/x/one" not in proj["repos"]


@requires_omnigraph
def test_project_update_refuses_status_completed(server, monkeypatch):
    """`project complete` seals a corpus trace, so it stays the only route —
    nothing should mint a trace without an outcome narrative."""
    import pytest

    from witan.cli._common import _fn
    from witan.cli.projects import project_update

    printed = _patch_server(monkeypatch, server)
    created = _fn(server.workflow_project_create)(title="seal me", description="d")

    with pytest.raises(SystemExit):
        project_update(created["slug"], status="completed")

    combined = "\n".join(printed)
    assert "project complete" in combined
    assert _fn(server.workflow_project_get)(created["slug"])["status"] == "active"


@requires_omnigraph
def test_project_update_has_no_phase_option(server):
    """Phase transitions stay behind `project advance`'s ordering check."""
    import inspect

    from witan.cli.projects import project_update

    assert "phase" not in inspect.signature(project_update).parameters


@requires_omnigraph
def test_project_update_unknown_slug_exits_nonzero(server, monkeypatch):
    import pytest

    from witan.cli.projects import project_update

    printed = _patch_server(monkeypatch, server)
    with pytest.raises(SystemExit):
        project_update("wp-does-not-exist", title="x")
    assert "No project" in "\n".join(printed)


@requires_omnigraph
def test_task_run_force_gets_past_a_held_task(server, monkeypatch):
    """`witan task run` used to dead-end on the state it reported.

    It had no `--force`, and the server-side interactive steal is unreachable
    from the CLI (`_fn` calls the tool with no `ctx`, so `elicit.confirm` always
    takes its non-interactive default). So a held task — including one held by
    nobody nameable — was reported and could not then be resolved from the same
    command; the user had to drop to `task_release`/`task_claim` by hand.
    """
    import pytest

    from witan import config as cfg_module
    from witan.cli import run_helpers

    printed = _patch_server(monkeypatch, server)
    launched = []
    monkeypatch.setattr(
        run_helpers, "_launch_agent", lambda *a, **kw: launched.append(a)
    )
    cfg = cfg_module.load()

    t = server.task_create(title="already held", description="x")
    server.task_claim(t["slug"], assignee="someone-else")

    with pytest.raises(SystemExit):
        run_helpers._run_task_slug(
            t["slug"], cfg=cfg, agent="claude", model=None, claim=True, dry_run=False
        )
    assert not launched
    # The refusal points at the way out rather than just naming the holder.
    assert "--force" in "\n".join(printed)

    run_helpers._run_task_slug(
        t["slug"],
        cfg=cfg,
        agent="claude",
        model=None,
        claim=True,
        force=True,
        dry_run=False,
    )
    assert launched
    assert server.task_get(t["slug"])["status"] == "in_progress"
