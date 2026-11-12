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
}


def _listed() -> set[str]:
    from witan import server as witan_server

    return {t.name for t in asyncio.run(witan_server.mcp._list_tools())}


@pytest.mark.parametrize("name", sorted(FOLDED))
def test_folded_tool_is_not_listed(name):
    assert name not in _listed(), f"{name} was folded into {FOLDED[name]}"
