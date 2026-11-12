"""Tools folded into another tool's parameters stay gone.

Each folded tool's capability is exercised through its new home in the suite
that owns that home (``test_tasks.py`` for ``task_link(remove=True)``, and so
on). This file pins the other half: the old names are not registered, so an
agent reading ``tools/list`` sees one way to do each thing.
"""

from __future__ import annotations

import asyncio

import pytest

#: Folded tool -> where its capability lives now.
FOLDED = {
    "task_unlink": "task_link(remove=True)",
    "workflow_project_search": "workflow_project_list(query=...)",
    "workflow_project_block": "workflow_project_update(add_blocked_by=...)",
    "workflow_project_unblock": "workflow_project_update(remove_blocked_by=...)",
    "workflow_project_get_blockers": 'workflow_project_get()["blockers"]',
    "topic_get": "recall(topic=...)",
    "symbol_context": "recall(symbol_id=...)",
    "memory_neighbors": "recall(memory=...)",
    "memory_symbols": "recall(memory=...)",
    "memory_for_contract": "recall(contract=..., contract_kind=...)",
}


def _listed() -> set[str]:
    from witan import server as witan_server

    return {t.name for t in asyncio.run(witan_server.mcp._list_tools())}


@pytest.mark.parametrize("name", sorted(FOLDED))
def test_folded_tool_is_not_listed(name):
    assert name not in _listed(), f"{name} was folded into {FOLDED[name]}"


def test_the_new_homes_declare_their_parameters():
    from witan import server as witan_server

    params = {
        t.name: set((t.parameters or {}).get("properties", {}))
        for t in asyncio.run(witan_server.mcp._list_tools())
    }
    assert "remove" in params["task_link"]
    assert "query" in params["workflow_project_list"]
    assert {"add_blocked_by", "remove_blocked_by"} <= params["workflow_project_update"]
    assert {"topic", "symbol_id", "memory", "contract", "contract_kind"} <= params[
        "recall"
    ]
