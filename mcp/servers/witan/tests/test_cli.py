"""End-to-end tests for CLI create subcommands (witan project create / witan task create)."""

from .conftest import requires_omnigraph


@requires_omnigraph
def test_project_create_minimal(server, monkeypatch):
    """project_create with just title/description creates a project in discovery phase."""
    from witan import cli as cli_mod
    from witan import server as srv

    printed = []
    monkeypatch.setattr(cli_mod, "_server", srv)
    monkeypatch.setattr(
        cli_mod.console, "print", lambda *a, **kw: printed.append(str(a[0]))
    )

    cli_mod.project_create(title="My Feature", description="build it")

    combined = "\n".join(printed)
    assert "wp-" in combined
    assert "discovery" in combined


@requires_omnigraph
def test_project_create_with_phase_and_repo(server, monkeypatch):
    """project_create accepts phase and repo overrides."""
    from witan import cli as cli_mod
    from witan import server as srv
    from witan.cli import _fn

    printed = []
    monkeypatch.setattr(cli_mod, "_server", srv)
    monkeypatch.setattr(
        cli_mod.console, "print", lambda *a, **kw: printed.append(str(a[0]))
    )

    cli_mod.project_create(
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

    # Verify the node actually exists in the graph.
    rows = _fn(srv.workflow_project_list)(repo="", status=None)
    assert any("spec" == r.get("phase") for r in rows)


@requires_omnigraph
def test_task_create_minimal(server, monkeypatch):
    """task_create_cmd with just title/description produces an open task."""
    from witan import cli as cli_mod
    from witan import server as srv

    printed = []
    monkeypatch.setattr(cli_mod, "_server", srv)
    monkeypatch.setattr(
        cli_mod.console, "print", lambda *a, **kw: printed.append(str(a[0]))
    )

    cli_mod.task_create_cmd(title="Do the thing", description="details here")

    combined = "\n".join(printed)
    assert "tk-" in combined
    assert "open" in combined


@requires_omnigraph
def test_task_create_with_project_and_blocker(server, monkeypatch):
    """task_create_cmd links to a project and a blocker correctly."""
    from witan import cli as cli_mod
    from witan import server as srv
    from witan.cli import _fn

    printed = []
    monkeypatch.setattr(cli_mod, "_server", srv)
    monkeypatch.setattr(
        cli_mod.console, "print", lambda *a, **kw: printed.append(str(a[0]))
    )

    blocker = _fn(srv.task_create)(title="blocker", description="x")
    proj = _fn(srv.workflow_project_create)(title="P", description="d")

    cli_mod.task_create_cmd(
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
    from witan import cli as cli_mod
    from witan import server as srv
    from witan.cli import _fn

    printed = []
    monkeypatch.setattr(cli_mod, "_server", srv)
    monkeypatch.setattr(
        cli_mod.console, "print", lambda *a, **kw: printed.append(str(a[0]))
    )

    epic = _fn(srv.task_create)(title="parent epic", description="x", type="epic")

    cli_mod.task_create_cmd(
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
