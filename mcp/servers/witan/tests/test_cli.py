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

    html = out.read_text()
    assert "vis-network" in html
    assert proj["slug"] in html
    assert blocker["slug"] in html
    assert blocked["slug"] in html
    assert '"group": "project"' in html
    assert '"group": "task"' in html
