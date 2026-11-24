"""The wave rule, on the cases of the web UI's ``ui/src/views/waves.test.ts``.

``witan/waves.py`` is the language server's twin of ``waves.ts``. Each test
here is named after the ``layout``/``components`` case it repeats, so a change
to one copy that the other does not follow fails on both sides.
"""

from __future__ import annotations

from witan.waves import components, layout

PROJECT = "wp-waves"


def task(slug: str, **patch) -> dict:
    return {
        "slug": slug,
        "title": slug,
        "status": "open",
        "priority": "p2",
        "project_slug": PROJECT,
        "blocked_by": None,
        "assignee": None,
        "claimed_at": None,
        **patch,
    }


def data(rows: list[dict], **patch) -> dict:
    """``task_ready`` for ``rows``, by its rule, so a test states only what it
    is about."""
    open_ = {r["slug"] for r in rows if r["status"] != "closed"}
    return {
        "tasks": rows,
        "ready": [
            r
            for r in rows
            if r["status"] != "closed"
            and (r["status"] != "in_progress" or r.get("lease_expired"))
            and not any(s in open_ for s in r["blocked_by"] or [])
        ],
        "outside": [],
        **patch,
    }


def wave_of(plan: dict) -> dict[str, int]:
    return {n["slug"]: n["wave"] for n in plan["nodes"]}


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


def test_draws_a_project_with_no_dependencies_as_a_single_wave():
    plan = layout(**data([task("tk-a"), task("tk-b"), task("tk-c")]))
    assert plan["waves"] == 1
    assert list(wave_of(plan).values()) == [0, 0, 0]
    assert plan["critical"] == []
    assert all(n["open_blockers"] == [] for n in plan["nodes"])


def test_places_each_task_one_wave_past_its_deepest_open_blocker():
    plan = layout(
        **data(
            [
                task("tk-a"),
                task("tk-b", blocked_by=["tk-a"]),
                task("tk-c", blocked_by=["tk-b"]),
                task("tk-d", blocked_by=["tk-a", "tk-c"]),
                task("tk-e"),
            ]
        )
    )
    assert wave_of(plan) == {"tk-a": 0, "tk-e": 0, "tk-b": 1, "tk-c": 2, "tk-d": 3}
    assert plan["waves"] == 4


def test_traces_the_longest_chain():
    plan = layout(
        **data(
            [
                task("tk-a"),
                task("tk-b", blocked_by=["tk-a"]),
                task("tk-c", blocked_by=["tk-b"]),
                task("tk-d", blocked_by=["tk-a"]),
            ]
        )
    )
    assert plan["critical"] == ["tk-a", "tk-b", "tk-c"]
    assert [n["slug"] for n in plan["nodes"] if n["critical"]] == [
        "tk-a",
        "tk-b",
        "tk-c",
    ]


def test_counts_the_work_waiting_on_a_wave_0_task():
    plan = layout(
        **data(
            [
                task("tk-a"),
                task("tk-b", blocked_by=["tk-a"]),
                task("tk-c", blocked_by=["tk-b"]),
                task("tk-x"),
                task("tk-y", blocked_by=["tk-x"]),
            ]
        )
    )
    downstream = {n["slug"]: n["downstream"] for n in plan["nodes"]}
    # Transitive: tk-c waits on tk-a through tk-b.
    assert downstream == {"tk-a": 2, "tk-b": 1, "tk-c": 0, "tk-x": 1, "tk-y": 0}


def test_does_not_wait_on_a_closed_blocker():
    plan = layout(
        **data([task("tk-a", status="closed"), task("tk-b", blocked_by=["tk-a"])])
    )
    assert wave_of(plan) == {"tk-b": 0}
    assert plan["nodes"][0]["open_blockers"] == []


def test_does_not_wait_on_a_blocker_that_no_longer_exists():
    plan = layout(**data([task("tk-b", blocked_by=["tk-gone"])]))
    assert wave_of(plan) == {"tk-b": 0}
    assert plan["disagree"] == []


def test_draws_an_open_blocker_from_another_project_as_a_stub():
    elsewhere = task("tk-elsewhere", project_slug="wp-other")
    plan = layout(
        tasks=[task("tk-b", blocked_by=["tk-elsewhere"])], ready=[], outside=[elsewhere]
    )
    stub = next(n for n in plan["nodes"] if n["slug"] == "tk-elsewhere")
    assert stub["outside"] is True
    assert stub["wave"] == 0
    assert wave_of(plan)["tk-b"] == 1
    assert next(n for n in plan["nodes"] if n["slug"] == "tk-b")["open_blockers"] == [
        "tk-elsewhere"
    ]
    assert plan["critical"] == ["tk-elsewhere", "tk-b"]
    assert plan["disagree"] == []
    # The stub is not the project's own: one wave of the project's work.
    assert plan["waves"] == 2


def test_reports_a_cycle_instead_of_looping_on_it():
    plan = layout(
        **data(
            [
                task("tk-a", blocked_by=["tk-b"]),
                task("tk-b", blocked_by=["tk-a"]),
                task("tk-c", blocked_by=["tk-b"]),
                task("tk-d"),
            ]
        )
    )
    assert plan["cycles"] == [["tk-a", "tk-b"]]
    assert wave_of(plan) == {"tk-a": 0, "tk-b": 0, "tk-d": 0, "tk-c": 1}
    assert sorted(n["slug"] for n in plan["nodes"] if n["cycle"]) == ["tk-a", "tk-b"]
    assert plan["disagree"] == []


def test_names_the_cycle_member_a_critical_path_leaves_through():
    plan = layout(
        **data(
            [
                task("tk-a"),
                task("tk-b", blocked_by=["tk-c"]),
                task("tk-c", blocked_by=["tk-a", "tk-b"]),
                task("tk-d", blocked_by=["tk-b"]),
            ]
        )
    )
    assert plan["critical"] == ["tk-a", "tk-c", "tk-b", "tk-d"]


def test_walks_every_member_between_where_a_path_enters_a_cycle_and_leaves_it():
    plan = layout(
        **data(
            [
                task("tk-a"),
                task("tk-x", blocked_by=["tk-a", "tk-y"]),
                task("tk-z", blocked_by=["tk-x"]),
                task("tk-y", blocked_by=["tk-z"]),
                task("tk-d", blocked_by=["tk-y"]),
            ]
        )
    )
    assert plan["critical"] == ["tk-a", "tk-x", "tk-z", "tk-y", "tk-d"]


def test_reports_a_task_that_blocks_itself():
    plan = layout(**data([task("tk-a", blocked_by=["tk-a"])]))
    assert plan["cycles"] == [["tk-a"]]


def test_agrees_with_task_ready_when_a_held_task_sits_in_wave_0():
    plan = layout(
        **data(
            [
                task(
                    "tk-held",
                    status="in_progress",
                    assignee="someone@mit.edu#abc",
                    lease_expired=False,
                ),
                task("tk-lapsed", status="in_progress", lease_expired=True),
            ]
        )
    )
    assert plan["disagree"] == []
    assert [(n["slug"], n["wave"], n["ready"]) for n in plan["nodes"]] == [
        ("tk-held", 0, False),
        ("tk-lapsed", 0, True),
    ]


def test_lists_where_task_ready_and_wave_0_disagree_in_both_directions():
    a = task("tk-a")
    b = task("tk-b", blocked_by=["tk-a"])
    plan = layout(tasks=[a, b], ready=[b], outside=[])
    assert sorted(plan["disagree"]) == ["tk-a", "tk-b"]


def test_lists_a_ready_task_the_chart_has_closed():
    plan = layout(
        tasks=[task("tk-a", status="closed")], ready=[task("tk-a")], outside=[]
    )
    assert plan["disagree"] == ["tk-a"]


def test_orders_rows_by_wave_the_critical_path_first_then_priority():
    plan = layout(
        **data(
            [
                task("tk-z", priority="p0"),
                task("tk-a", priority="p3"),
                task("tk-b", blocked_by=["tk-a"]),
            ]
        )
    )
    assert [n["slug"] for n in plan["nodes"]] == ["tk-a", "tk-z", "tk-b"]
