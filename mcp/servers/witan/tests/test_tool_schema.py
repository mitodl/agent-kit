"""Every MCP tool parameter must carry a description in its generated schema.

WHY THIS IS A TEST AND NOT A DOCS CONCERN. FastMCP builds each tool's JSON
Schema from its docstring's numpydoc ``Parameters`` section, and that schema is
what reaches the model. A parameter with no entry there is not merely
undocumented — the agent choosing arguments for it sees only a name and a type.
62 of 200 parameters were in that state before this suite existed, and nothing
would have reported them drifting back: the existing ``list_tools`` coverage
asserts tool *names*, so a docstring edit, a decorator change, or a parser
change in FastMCP could restore the gap silently.

Asserted over the whole registered surface rather than a fixed list, so a newly
added tool is covered the moment it is registered rather than when someone
remembers to extend a fixture.
"""

from __future__ import annotations

import asyncio
import re


def _tool_properties() -> list[tuple[str, str, dict]]:
    """(tool, parameter, schema) for every exposed parameter of every tool."""
    from witan import server as witan_server

    tools = asyncio.run(witan_server.mcp._list_tools())
    return [
        (tool.name, name, schema)
        for tool in tools
        for name, schema in ((tool.parameters or {}).get("properties", {})).items()
    ]


def test_every_tool_parameter_has_a_description():
    missing = [
        f"{tool}.{param}"
        for tool, param, schema in _tool_properties()
        if not (schema.get("description") or "").strip()
    ]
    assert not missing, (
        f"{len(missing)} tool parameter(s) reach the model with no description. "
        "Add a numpydoc `Parameters` entry for each in witan/server.py:\n  "
        + "\n  ".join(sorted(missing))
    )


def test_the_tool_surface_is_actually_being_inspected():
    """Guard the guard: an empty listing would make the assertion above vacuous.

    If a refactor changed how tools are registered — or ``_list_tools`` moved —
    the test above would pass on an empty list and report nothing, which is the
    same output as success. This fails instead.
    """
    properties = _tool_properties()
    assert len(properties) > 100, (
        f"only {len(properties)} tool parameters found; the tool surface is "
        "~200 parameters, so the schema inspection is probably broken rather "
        "than the surface having shrunk this far"
    )


def test_parameters_carry_the_names_agents_guess():
    """Transcripts showed agents passing ``body`` to ``task_comment`` and
    ``limit`` to ``task_search``; both are the real names now, and the session
    id ``workflow_session_end`` is often handed is ruled out by name."""
    params = {(tool, param): schema for tool, param, schema in _tool_properties()}

    assert ("task_comment", "body") in params
    assert ("task_comment", "text") not in params
    assert params[("task_search", "limit")]["default"] == 20
    assert (
        "session_id" in params[("workflow_session_end", "session_slug")]["description"]
    )


def test_every_tool_carries_annotations():
    """Clients decide auto-approval from ``readOnlyHint``/``destructiveHint``;
    a tool without them is treated as an unknown, possibly destructive write.
    ``_tool`` requires annotations, and this covers anything registered
    around it."""
    from witan import server as witan_server

    tools = asyncio.run(witan_server.mcp._list_tools())
    assert len(tools) > 30
    unannotated = sorted(
        tool.name
        for tool in tools
        if tool.annotations is None
        or tool.annotations.read_only_hint is None
        or (
            not tool.annotations.read_only_hint
            and tool.annotations.destructive_hint is None
        )
    )
    assert not unannotated, f"tools missing MCP annotations: {unannotated}"


def test_the_tools_that_overwrite_or_remove_are_annotated_destructive():
    """Pinned rather than derived, so loosening one of these to a plain write
    is a visible test change rather than a silent one."""
    from witan import server as witan_server

    tools = asyncio.run(witan_server.mcp._list_tools())
    destructive = {
        t.name
        for t in tools
        if not t.annotations.read_only_hint and t.annotations.destructive_hint
    }
    assert destructive == {
        "claim_authorship",
        "memory_delete",
        "memory_link",
        "memory_update",
        "store_merge",
        "task_close",
        "task_link",
        "task_release",
        "task_update",
        "workflow_project_advance",
        "workflow_project_complete",
        "workflow_project_update",
        "workflow_session_end",
        "workflow_session_update",
    }


def test_the_local_read_allowlist_is_annotated_read_only():
    """``READ_TOOLS`` lets the CLI fall back to a local store; a tool on it that
    is annotated as a write means one of the two is wrong."""
    from witan import server as witan_server
    from witan.cli.local_dispatch import READ_TOOLS

    tools = {t.name: t for t in asyncio.run(witan_server.mcp._list_tools())}
    assert (
        sorted(n for n in READ_TOOLS if not tools[n].annotations.read_only_hint) == []
    )


def _enum_values(schema: dict) -> set[str]:
    """The string values an optional ``Literal`` parameter's schema admits."""
    return {v for part in schema.get("anyOf", [schema]) for v in part.get("enum", [])}


def _stored_enum(field: str) -> set[str]:
    """The values ``WorkflowSession.<field>``'s ``enum(...)`` in schema.pg allows."""
    from .conftest import SCHEMA

    node = re.search(r"node WorkflowSession \{(.*?)\n\}", SCHEMA.read_text(), re.DOTALL)
    assert node is not None
    declared = re.search(rf"\b{field}:\s+enum\(([^)]*)\)", node[1])
    assert declared is not None
    return {v.strip() for v in declared[1].split(",")}


def test_fanout_state_and_mode_admit_exactly_the_schema_enums():
    """A runner reaches these tools through MCP, where the advertised enum is
    what refuses a bad value before it is written. It has to match the node's
    ``enum(...)`` in schema.pg, or one side accepts what the other refuses."""
    params = {(tool, param): schema for tool, param, schema in _tool_properties()}
    state, mode = _stored_enum("state"), _stored_enum("mode")

    assert state == {"starting", "running", "exited", "crashed", "cancelled"}
    assert mode == {"oneshot", "resume", "live"}
    for tool in ("workflow_session_start", "workflow_session_update"):
        assert _enum_values(params[(tool, "state")]) == state
    # Ending takes the same values; `workflow_session_end` refuses the
    # non-terminal ones itself.
    assert _enum_values(params[("workflow_session_end", "state")]) == state
    assert _enum_values(params[("workflow_session_start", "mode")]) == mode


def test_a_session_cannot_move_hosts_or_worktrees():
    """``host`` and ``cwd`` locate the harness's stored session for a resume.
    They are set at start and nothing may change them afterwards."""
    params = {(tool, param) for tool, param, _ in _tool_properties()}

    assert {("workflow_session_start", "host"), ("workflow_session_start", "cwd")} <= (
        params
    )
    assert ("workflow_session_update", "host") not in params
    assert ("workflow_session_update", "cwd") not in params
