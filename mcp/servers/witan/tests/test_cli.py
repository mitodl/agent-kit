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

    fake = _patch_mcp(monkeypatch)
    serve(transport="streamable-http", host="0.0.0.0", port=9001, path="/witan")
    assert fake.run_calls == [
        {
            "transport": "streamable-http",
            "host": "0.0.0.0",
            "port": 9001,
            "path": "/witan",
        }
    ]


def test_serve_http_prepends_missing_leading_slash(monkeypatch):
    from witan.cli import serve

    fake = _patch_mcp(monkeypatch)
    serve(transport="http", path="mcp")
    assert fake.run_calls[0]["path"] == "/mcp"
