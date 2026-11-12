"""Admin and corpus-mining tools are listed to witan's CLI and to nobody else.

``witan serve`` over stdio and the deployment over streamable HTTP serve the same
``witan.server.mcp`` instance, and the filter is middleware on that instance, so
an in-memory client exercises the same ``tools/list`` path both transports do.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from fastmcp import Client
from mcp.types import Implementation

from witan.cli_only import CLI_ONLY_TOOLS
from witan_core.remote.proxy import proxy_client_info

from .conftest import requires_omnigraph
from .test_cli import _patch_server


def _listed(client_info: Implementation | None = None) -> set[str]:
    import witan.server as srv

    async def _list() -> set[str]:
        async with Client(srv.mcp, client_info=client_info) as client:
            return {t.name for t in await client.list_tools()}

    return asyncio.run(_list())


def test_cli_only_tools_are_absent_from_an_agents_tools_list(server):
    listed = _listed(Implementation(name="claude-code", version="2.1.0"))
    assert listed, "expected the server to list some tools"
    assert not (CLI_ONLY_TOOLS & listed)


def test_cli_only_tools_are_absent_for_a_client_that_names_no_one(server):
    assert not (CLI_ONLY_TOOLS & _listed())


def test_the_cli_proxy_is_listed_every_cli_only_tool(server):
    # Also what keeps CLI_ONLY_TOOLS from naming a tool that no longer exists.
    assert CLI_ONLY_TOOLS <= _listed(proxy_client_info())


@requires_omnigraph
def test_an_unlisted_tool_is_still_callable_by_name(server):
    """The filter is discoverability, not access control: ``tools/call`` is
    untouched, which is also what an agent with a stale tool list relies on."""
    import witan.server as srv

    async def _call():
        async with Client(srv.mcp) as client:
            return await client.call_tool("workflow_trace_list", {"repo": ""})

    assert asyncio.run(_call()).is_error is False


@requires_omnigraph
def test_trace_annotate_cli_records_slugs(server, monkeypatch):
    from witan.cli.traces import trace_annotate_cmd

    proj = server.workflow_project_create(title="annotate via cli", description="d")
    server.workflow_project_complete(proj["slug"], outcome="done")

    printed = _patch_server(monkeypatch, server)
    trace_annotate_cmd(proj["slug"], lesson=["les-a"], pattern=["pat-a", "pat-b"])

    assert "1 lesson(s), 2 pattern(s)" in "\n".join(printed)
    trace = server.workflow_trace_get(proj["slug"])
    assert trace["lessons_slug"] == ["les-a"]
    assert trace["patterns_slug"] == ["pat-a", "pat-b"]


@requires_omnigraph
def test_trace_mine_cli_prints_material_then_creates_memories(
    server, monkeypatch, tmp_path
):
    from witan.cli.traces import trace_mine_cmd

    proj = server.workflow_project_create(title="mine via cli", description="d")
    sess = server.workflow_session_start(
        project_slug=proj["slug"], session_id=uuid.uuid4().hex, phase="discovery"
    )
    server.workflow_session_end(sess["session_slug"], summary="learned a thing")
    done = server.workflow_project_complete(proj["slug"], outcome="shipped it")

    printed = _patch_server(monkeypatch, server)
    trace_mine_cmd(done["trace_slug"])
    material = "\n".join(printed)
    assert "shipped it" in material
    assert "learned a thing" in material

    proposals = tmp_path / "proposals.json"
    proposals.write_text(
        json.dumps(
            {
                "patterns": [{"title": "a pattern", "content": "do X because Y"}],
                "lessons": [{"title": "a lesson", "content": "watch out for Z"}],
            }
        )
    )
    printed.clear()
    trace_mine_cmd(proj["slug"], proposals=proposals)

    trace = server.workflow_trace_get(done["trace_slug"])
    assert len(trace["patterns_slug"]) == 1
    assert len(trace["lessons_slug"]) == 1
    created = "\n".join(printed)
    assert trace["patterns_slug"][0] in created
    assert trace["lessons_slug"][0] in created
