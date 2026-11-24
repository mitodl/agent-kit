"""The wave rule, on the cases it shares with the web UI's waves view.

``witan/waves.py`` is the language server's twin of ``ui/src/views/waves.ts``.
``ui/src/views/waves.cases.json`` holds the ``layout`` cases of
``waves.test.ts`` with what this module answers for each, and both suites run
every case, so a rule changed in one copy only fails in the other's tests.
The ``components`` cases are repeated here by hand.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from witan.waves import components, layout

SHARED = json.loads(
    (
        Path(__file__).parents[1] / "ui" / "src" / "views" / "waves.cases.json"
    ).read_text()
)


# ── components ───────────────────────────────────────────────────────────


def test_orders_components_blockers_first():
    nxt = {"tk-a": ["tk-b"], "tk-b": ["tk-c"]}
    assert components(["tk-c", "tk-b", "tk-a"], nxt) == [["tk-a"], ["tk-b"], ["tk-c"]]


def test_collapses_a_cycle_into_one_component():
    nxt = {"tk-a": ["tk-b"], "tk-b": ["tk-a", "tk-c"]}
    assert components(["tk-a", "tk-b", "tk-c"], nxt) == [["tk-a", "tk-b"], ["tk-c"]]


def test_walks_a_chain_far_longer_than_the_call_stack_is_deep():
    length = 50_000
    slugs = [f"tk-{at}" for at in range(length)]
    nxt = {slug: [slugs[at + 1]] for at, slug in enumerate(slugs[:-1])}
    found = components(slugs, nxt)
    assert len(found) == length
    assert found[0] == ["tk-0"]
    assert found[-1] == [f"tk-{length - 1}"]


# ── layout ───────────────────────────────────────────────────────────────


def _row(patch: dict) -> dict:
    return {"title": patch["slug"], **SHARED["defaults"], **patch}


@pytest.mark.parametrize("case", SHARED["cases"], ids=lambda c: c["name"])
def test_layout_matches_the_waves_view(case):
    tasks = [_row(t) for t in case["tasks"]]
    by_slug = {t["slug"]: t for t in tasks}
    plan = layout(
        tasks,
        [by_slug.get(slug) or _row({"slug": slug}) for slug in case["ready"]],
        [_row(t) for t in case["outside"]],
    )
    nodes = plan["nodes"]

    def slugs(flag: str) -> list[str]:
        return [n["slug"] for n in nodes if n[flag]]

    assert {
        "order": [n["slug"] for n in nodes],
        "wave": {n["slug"]: n["wave"] for n in nodes},
        "outside": slugs("outside"),
        "cycle": slugs("cycle"),
        "critical_nodes": slugs("critical"),
        "downstream": {n["slug"]: n["downstream"] for n in nodes},
        "ready": slugs("ready"),
        "waves": plan["waves"],
        "critical": plan["critical"],
        "cycles": plan["cycles"],
        "disagree": sorted(plan["disagree"]),
    } == case["expect"]


def test_each_node_names_its_open_blockers_in_and_outside_the_project():
    plan = layout(
        [
            _row({"slug": "tk-a"}),
            _row({"slug": "tk-b", "blocked_by": ["tk-elsewhere", "tk-a", "tk-gone"]}),
            _row({"slug": "tk-c", "status": "closed"}),
            _row({"slug": "tk-d", "blocked_by": ["tk-c"]}),
        ],
        [],
        [_row({"slug": "tk-elsewhere", "project_slug": "wp-other"})],
    )
    assert {n["slug"]: n["open_blockers"] for n in plan["nodes"]} == {
        "tk-elsewhere": [],
        "tk-a": [],
        "tk-d": [],
        "tk-b": ["tk-elsewhere", "tk-a"],
    }
