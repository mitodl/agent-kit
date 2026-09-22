"""MCP Apps widgets for the four bound read tools (spec §7).

Three properties:

  * a tool is bound only when its widget file exists, so a source install with
    no frontend build never names a resource it cannot serve;
  * the resource is served as an MCP App, from the file on disk;
  * binding a tool changes nothing about what the tool returns. Claude Code
    renders no widgets and shows only the text result, so that result has to
    be byte-identical with and without the binding (spec §7.2).

Built against temporary widget files rather than `witan/ui_dist/widgets`, which
is a build output the test run has no reason to have.
"""

import asyncio
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP
from witan import server as srv
from witan import ui_widgets

from .conftest import _unwrap, requires_omnigraph


@pytest.fixture
def widgets(tmp_path):
    """A stand-in for what `build-widgets.js` writes."""
    root = tmp_path / "widgets"
    root.mkdir()
    for tool in ui_widgets.BOUND_TOOLS:
        (root / f"{tool}.html").write_text(f"<!doctype html><title>{tool}</title>")
    return root


def test_the_frontend_builds_a_widget_for_exactly_the_bound_tools():
    """The CI bundle checks list widgets from the frontend's entries, so this is
    what keeps them checking the set the server binds."""
    entries = Path(__file__).parents[1] / "ui" / "widgets"

    assert {p.stem for p in entries.glob("*.html")} == set(ui_widgets.BOUND_TOOLS)


def test_a_tool_is_bound_only_when_its_widget_was_built(tmp_path, widgets):
    assert ui_widgets.app_for("task_ready", tmp_path / "nothing-here") is None

    app = ui_widgets.app_for("task_ready", widgets)

    assert app is not None
    assert app.resource_uri == "ui://witan/task_ready.html"
    # No CSP, so the host applies `connect-src 'none'`: a widget fetches nothing.
    assert app.csp is None


def test_a_tool_outside_the_bound_set_has_no_widget(widgets):
    with pytest.raises(ValueError, match="task_get has no widget"):
        ui_widgets.app_for("task_get", widgets)


def test_the_real_server_binds_exactly_what_was_built():
    """Holds whether or not this checkout ran `npm run build`."""

    async def metas():
        async with Client(srv.mcp) as client:
            return {
                tool.name: (tool.meta or {}).get("ui")
                for tool in await client.list_tools()
            }

    ui = asyncio.run(metas())

    for tool in ui_widgets.BOUND_TOOLS:
        app = ui_widgets.app_for(tool)
        expected = None if app is None else {"resourceUri": app.resource_uri}
        assert ui[tool] == expected, tool
    assert not {name for name, meta in ui.items() if meta} - set(ui_widgets.BOUND_TOOLS)


def test_only_built_widgets_register_a_resource(tmp_path, widgets):
    (widgets / "recall.html").unlink()
    mcp = FastMCP("witan-widgets-test")

    assert ui_widgets.register(mcp, widgets) == [
        "task_ready",
        "workflow_project_status",
        "task_list",
    ]
    assert ui_widgets.register(FastMCP("empty"), tmp_path / "nothing-here") == []


def test_a_widget_is_served_as_an_mcp_app_from_disk(widgets):
    mcp = FastMCP("witan-widgets-test")
    ui_widgets.register(mcp, widgets)
    # Rebuilt after registration: read per request, not cached at import.
    (widgets / "task_ready.html").write_text("<!doctype html><p>rebuilt</p>")

    async def read():
        async with Client(mcp) as client:
            return await client.read_resource("ui://witan/task_ready.html")

    [content] = asyncio.run(read())

    assert content.mime_type == "text/html;profile=mcp-app"
    assert content.text == "<!doctype html><p>rebuilt</p>"


@requires_omnigraph
def test_binding_leaves_every_bound_tools_result_byte_identical(server, widgets):
    """The Claude Code case: no widget rendered, so the text IS the result.

    Each tool's own registered function goes onto two servers, one bound and
    one not, and the two results are compared whole, text and structured.
    """
    project = server.workflow_project_create(title="p", description="d")
    task = server.task_create(title="t", description="d", project_slug=project["slug"])
    server.task_create(title="u", description="d", blocked_by=[task["slug"]])
    server.memory_store(kind="pattern", title="widgets", content="render a result")

    calls = {
        "task_ready": {"repo": ""},
        "task_list": {"repo": ""},
        "recall": {"query": "widgets", "repo": ""},
        "workflow_project_status": {"slug": project["slug"]},
    }
    assert set(calls) == set(ui_widgets.BOUND_TOOLS)

    async def call(tool, app):
        mcp = FastMCP("witan-widgets-test")
        mcp.tool(_unwrap(getattr(srv, tool)), app=app)
        async with Client(mcp) as client:
            return await client.call_tool(tool, calls[tool])

    for tool in ui_widgets.BOUND_TOOLS:
        plain = asyncio.run(call(tool, None))
        bound = asyncio.run(call(tool, ui_widgets.app_for(tool, widgets)))

        assert [c.text for c in bound.content] == [c.text for c in plain.content], tool
        assert bound.structured_content == plain.structured_content, tool
        assert plain.content and plain.content[0].text, tool
