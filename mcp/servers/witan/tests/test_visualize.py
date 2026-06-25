"""Unit tests for witan.visualize — no graph store required."""

from witan import visualize

P1_SLUG = "wp-test-project-aaaa01"
P2_SLUG = "wp-done-project-bbbb02"
T1_SLUG = "tk-open-task-cccc03"
T2_SLUG = "tk-blocked-task-dddd04"
T3_SLUG = "tk-child-task-eeee05"

PROJECTS = [
    {
        "slug": P1_SLUG,
        "title": "Active Project",
        "status": "active",
        "phase": "discovery",
        "description": "an active project",
        "repos": ["https://github.com/test/repo"],
    },
    {
        "slug": P2_SLUG,
        "title": "Done Project",
        "status": "completed",
        "phase": "delivery",
        "description": "a completed project",
        "repos": [],
    },
]

TASKS = [
    {
        "slug": T1_SLUG,
        "title": "Open Task",
        "status": "open",
        "priority": "p1",
        "project_slug": P1_SLUG,
        "parent_slug": None,
        "blocked_by": None,
        "description": "an open task",
    },
    {
        "slug": T2_SLUG,
        "title": "Blocked Task",
        "status": "blocked",
        "priority": "p0",
        "project_slug": P1_SLUG,
        "parent_slug": None,
        "blocked_by": [T1_SLUG],
        "description": "a blocked task",
    },
    {
        "slug": T3_SLUG,
        "title": "Child Task",
        "status": "open",
        "priority": "p2",
        "project_slug": P1_SLUG,
        "parent_slug": T1_SLUG,
        "blocked_by": None,
        "description": "a child task",
    },
]


def test_build_graph_node_counts():
    g = visualize.build_graph(PROJECTS, TASKS)
    assert len([n for n in g.nodes if n.group == "project"]) == 2
    assert len([n for n in g.nodes if n.group == "task"]) == 3


def test_build_graph_project_nodes_colored_by_status():
    g = visualize.build_graph(PROJECTS, TASKS)
    p_nodes = {n.id: n for n in g.nodes if n.group == "project"}
    assert p_nodes[P1_SLUG].color == visualize._PROJECT_COLORS["active"]
    assert p_nodes[P2_SLUG].color == visualize._PROJECT_COLORS["completed"]


def test_build_graph_task_nodes_colored_by_status():
    g = visualize.build_graph(PROJECTS, TASKS)
    t_nodes = {n.id: n for n in g.nodes if n.group == "task"}
    assert t_nodes[T1_SLUG].color == visualize._TASK_COLORS["open"]
    assert t_nodes[T2_SLUG].color == visualize._TASK_COLORS["blocked"]


def test_build_graph_priority_prefixes_high_priority_label():
    g = visualize.build_graph(PROJECTS, TASKS)
    t_nodes = {n.id: n for n in g.nodes if n.group == "task"}
    assert t_nodes[T1_SLUG].label.startswith("[p1]")
    assert t_nodes[T2_SLUG].label.startswith("[p0]")
    assert not t_nodes[T3_SLUG].label.startswith("[p")


def test_build_graph_edges_belongs_to():
    g = visualize.build_graph(PROJECTS, TASKS)
    belongs = [(e.src, e.dst) for e in g.edges if e.kind == "belongs_to"]
    assert (T1_SLUG, P1_SLUG) in belongs
    assert (T2_SLUG, P1_SLUG) in belongs
    assert (T3_SLUG, P1_SLUG) in belongs


def test_build_graph_no_belongs_to_when_suppressed():
    g = visualize.build_graph(PROJECTS, TASKS, show_belongs_to=False)
    assert all(e.kind != "belongs_to" for e in g.edges)


def test_build_graph_edges_parent_child():
    g = visualize.build_graph(PROJECTS, TASKS)
    parent_edges = [(e.src, e.dst) for e in g.edges if e.kind == "parent"]
    assert (T3_SLUG, T1_SLUG) in parent_edges


def test_build_graph_edges_blocks():
    g = visualize.build_graph(PROJECTS, TASKS)
    block_edges = [(e.src, e.dst) for e in g.edges if e.kind == "blocks"]
    # T1 blocks T2 (T2.blocked_by contains T1)
    assert (T1_SLUG, T2_SLUG) in block_edges


def test_build_graph_missing_project_omits_belongs_to():
    """Tasks whose project_slug doesn't appear in projects get no belongs_to edge."""
    tasks = [
        {
            "slug": T1_SLUG,
            "title": "Orphan",
            "status": "open",
            "priority": "p2",
            "project_slug": "wp-nonexistent-xxxx",
            "parent_slug": None,
            "blocked_by": None,
        }
    ]
    g = visualize.build_graph([], tasks)
    assert all(e.kind != "belongs_to" for e in g.edges)


def test_render_html_writes_valid_structure(tmp_path):
    g = visualize.build_graph(PROJECTS, TASKS)
    out = visualize.render_html(g, tmp_path / "graph.html")
    text = out.read_text(encoding="utf-8")
    assert out.exists()
    assert "vis-network" in text
    assert P1_SLUG in text
    assert T1_SLUG in text
    assert '"group": "project"' in text
    assert '"group": "task"' in text
    assert "blocks" in text


def test_render_html_empty_graph(tmp_path):
    g = visualize.build_graph([], [])
    out = visualize.render_html(g, tmp_path / "empty.html")
    assert out.exists()
    assert "vis-network" in out.read_text(encoding="utf-8")


def test_render_dot_writes_dot_syntax(tmp_path):
    g = visualize.build_graph(PROJECTS, TASKS)
    out = visualize.render_dot(g, tmp_path / "graph.dot")
    text = out.read_text(encoding="utf-8")
    assert out.exists()
    assert "digraph witan_workflow" in text
    assert P1_SLUG in text
    assert T1_SLUG in text
    assert "->" in text


def test_render_rich_smoke():
    from io import StringIO

    from rich.console import Console

    g = visualize.build_graph(PROJECTS, TASKS)
    buf = StringIO()
    visualize.render_rich(g, Console(file=buf, highlight=False))
    out = buf.getvalue()
    assert "Active Project" in out
    assert "Open Task" in out


def test_render_rich_empty():
    from io import StringIO

    from rich.console import Console

    g = visualize.build_graph([], [])
    buf = StringIO()
    visualize.render_rich(g, Console(file=buf))
    assert "No projects or tasks" in buf.getvalue()
