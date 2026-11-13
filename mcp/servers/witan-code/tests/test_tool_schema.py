"""Every `code_*` tool parameter must carry a description in its schema.

The witan-council twin of this file explains the reasoning at length; briefly:
FastMCP derives each tool's JSON Schema from its docstring's numpydoc
``Parameters`` section, and that schema is what the calling model sees. Seven
parameters here — including the ``symbol_id`` of ``code_callers``,
``code_find_references``, ``code_impact`` and ``code_cross_repo_impact`` —
reached the model with nothing but a name and a type, and the existing
``list_tools`` coverage asserts only that ``code_reindex`` exists, so a
regression would not have been reported.
"""

from __future__ import annotations

import asyncio


def _tool_properties() -> list[tuple[str, str, dict]]:
    """(tool, parameter, schema) for every exposed parameter of every tool."""
    from witan_code import server as code_server

    tools = asyncio.run(code_server.mcp._list_tools())
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
        "Add a numpydoc `Parameters` entry for each in witan_code/server.py:\n  "
        + "\n  ".join(sorted(missing))
    )


def test_the_tool_surface_is_actually_being_inspected():
    """Guard the guard — an empty listing would make the assertion vacuous."""
    properties = _tool_properties()
    assert len(properties) > 20, (
        f"only {len(properties)} tool parameters found; the code_* surface is "
        "larger than that, so the schema inspection is probably broken"
    )


def test_every_tool_carries_annotations():
    """Every ``code_*`` tool, the indexer's store tools included, says whether
    it reads, and if it writes, whether it can destroy."""
    from witan_code import server as code_server
    from witan_code.remote.store import _READ_ONLY_STORE_TOOLS

    # The store tools are read off the registration table rather than
    # registered here: registering mutates the module's server for every later
    # test in the process.
    annotations = {
        t.name: t.annotations for t in asyncio.run(code_server.mcp._list_tools())
    }
    annotations |= {tool.__name__: hints for tool, hints in code_server._STORE_TOOLS}
    assert len(annotations) > 20
    unannotated = sorted(
        name
        for name, hints in annotations.items()
        if hints is None
        or hints.read_only_hint is None
        or (not hints.read_only_hint and hints.destructive_hint is None)
    )
    assert not unannotated, f"tools missing MCP annotations: {unannotated}"
    assert all(annotations[n].read_only_hint for n in _READ_ONLY_STORE_TOOLS)
