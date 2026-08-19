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
