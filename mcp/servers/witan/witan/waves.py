"""Dependency waves: how many serialized rounds of work remain in a project.

The editor-neutral twin of the web UI's waves view
(``ui/src/views/waves.ts``), which keeps its own copy because the UI draws
from one read in the browser. An editor client cannot hold the rule (ADR 0012
keeps logic in the language server), so ``witan lsp``'s
``witan.listProjectTasks`` lays a project out with this module.
``tests/test_waves.py`` runs the cases of ``waves.test.ts``, so a change to one
copy that the other does not follow fails there.

A task with no open blocker is wave 0, and every other task sits one wave past
its deepest open blocker. Blocker status follows ``task_ready``'s resolver: a
blocker that no longer exists holds nothing back, and a row with no status is
open. An open blocker from another project enters as a stub, and a task in a
``Blocks`` cycle is marked, since it can never become ready as the graph
stands. Readiness is not decided here: wave 0 is "no open blocker", and
``task_ready`` is that plus "nobody holds it", so each node carries whether
``task_ready`` returned it and ``disagree`` lists where the two differ.

One difference from the TypeScript: ties in row order fall back to the slug by
code point, where ``localeCompare`` orders by locale. Slugs are lowercase
ASCII, so the two differ only where punctuation and letters tie at a position.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

PRIORITY_RANK = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}

Row = Mapping[str, Any]


def _open_blockers(task: Row, status: Mapping[str, str | None]) -> list[str]:
    """Every blocker of ``task`` still open. A blocker missing from ``status``
    is gone, or closed outside the project (only open outside ones are
    passed in)."""
    return [
        slug
        for slug in task.get("blocked_by") or []
        if slug in status and (status[slug] or "open") != "closed"
    ]


def components(slugs: Iterable[str], next_: Mapping[str, list[str]]) -> list[list[str]]:
    """Strongly connected components, dependencies before dependents.

    Tarjan's algorithm, iterative so a long chain cannot exhaust the stack,
    with edges pointing blocker to dependent. It emits dependents first, so
    the result is reversed. A component of more than one task, or one that
    blocks itself, is a cycle.
    """
    counter = 0
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    found: list[list[str]] = []

    def enter(slug: str) -> None:
        nonlocal counter
        index[slug] = low[slug] = counter
        counter += 1
        stack.append(slug)
        on_stack.add(slug)

    for root in slugs:
        if root in index:
            continue
        # Each frame is a task and how many of its dependents it has walked.
        frames = [[root, 0]]
        enter(root)
        while frames:
            frame = frames[-1]
            slug, walked = frame
            targets = next_.get(slug, [])
            if walked < len(targets):
                to = targets[walked]
                frame[1] += 1
                if to not in index:
                    enter(to)
                    frames.append([to, 0])
                elif to in on_stack:
                    low[slug] = min(low[slug], index[to])
                continue
            frames.pop()
            if frames:
                caller = frames[-1][0]
                low[caller] = min(low[caller], low[slug])
            if low[slug] == index[slug]:
                component = []
                while True:
                    popped = stack.pop()
                    on_stack.discard(popped)
                    component.append(popped)
                    if popped == slug:
                        break
                found.append(sorted(component))
    found.reverse()
    return found


def _by_priority(task: Row) -> tuple[int, str]:
    return PRIORITY_RANK[task["priority"]], task["slug"]


def layout(
    tasks: Sequence[Row], ready: Sequence[Row], outside: Sequence[Row]
) -> dict[str, Any]:
    """Lay a project's tasks out in waves.

    :param tasks: Every task in the project, closed ones included.
    :param ready: ``task_ready`` for the project; checked against wave 0,
        never used to place.
    :param outside: The open blockers from outside the project.
    :returns: ``nodes``, each task row with ``wave``, ``outside``, ``cycle``,
        ``critical``, ``downstream`` (open tasks waiting on it, directly or
        transitively), ``ready`` and ``open_blockers``, in row order (by wave,
        outside stubs first, then the critical path, then priority);
        ``waves``, how many waves the project's own tasks span; ``critical``,
        the longest chain's slugs, first to last; ``cycles``, each cycle's
        members; and ``disagree``, the slugs ``task_ready`` and wave 0 place
        differently.
    :rtype: dict
    """
    status = {t["slug"]: t.get("status") for t in [*outside, *tasks]}
    open_tasks = [t for t in tasks if t.get("status") != "closed"]
    rows: dict[str, tuple[Row, bool]] = {t["slug"]: (t, False) for t in open_tasks}
    stubs = {t["slug"]: t for t in outside}

    # Edges run blocker -> dependent. An outside blocker only enters as the
    # tail of one, so a stub appears exactly when something waits on it.
    next_: dict[str, list[str]] = {}
    prev: dict[str, list[str]] = {}
    for task in open_tasks:
        for blocker in _open_blockers(task, status):
            if blocker not in rows:
                if blocker not in stubs:
                    continue
                rows[blocker] = (stubs[blocker], True)
            next_.setdefault(blocker, []).append(task["slug"])
            prev.setdefault(task["slug"], []).append(blocker)

    slugs = sorted(rows)
    order = components(slugs, next_)
    component_of = {slug: at for at, members in enumerate(order) for slug in members}
    cycles = [
        members
        for members in order
        if len(members) > 1 or members[0] in next_.get(members[0], [])
    ]
    in_cycle = {slug for members in cycles for slug in members}

    # Longest path from a source, over components in dependency order, so
    # every predecessor's wave is final before it is read.
    wave: dict[str, int] = {}
    for members in order:
        depth = max(
            (
                wave[source] + 1
                for slug in members
                for source in prev.get(slug, [])
                if component_of[source] != component_of[slug]
            ),
            default=0,
        )
        for slug in members:
            wave[slug] = depth

    downstream = {}
    for slug in slugs:
        seen: set[str] = set()
        pending = list(next_.get(slug, []))
        while pending:
            at = pending.pop()
            if at not in seen:
                seen.add(at)
                pending.extend(next_.get(at, []))
        seen.discard(slug)
        downstream[slug] = len(seen)

    critical = _critical_path(slugs, rows, prev, next_, wave, component_of)
    on_path = set(critical)
    ready_slugs = {t["slug"] for t in ready}

    nodes = [
        {
            **rows[slug][0],
            "outside": rows[slug][1],
            "wave": wave[slug],
            "cycle": slug in in_cycle,
            "critical": slug in on_path,
            "downstream": downstream[slug],
            "ready": slug in ready_slugs,
            "open_blockers": prev.get(slug, []),
        }
        for slug in slugs
    ]
    nodes.sort(
        key=lambda n: (
            n["wave"],
            not n["outside"],
            not n["critical"],
            _by_priority(n),
        )
    )
    project = [n for n in nodes if not n["outside"]]
    return {
        "nodes": nodes,
        "waves": max((n["wave"] for n in project), default=-1) + 1,
        "critical": critical,
        "cycles": cycles,
        "disagree": _disagreements(project, ready),
    }


def _critical_path(
    slugs: list[str],
    rows: Mapping[str, tuple[Row, bool]],
    prev: Mapping[str, list[str]],
    next_: Mapping[str, list[str]],
    wave: Mapping[str, int],
    component_of: Mapping[str, int],
) -> list[str]:
    """The longest chain of project work, first task to last.

    Traced back from the deepest project task, one blocker a wave shallower
    at a time; ties go to the higher priority, then the slug. A path that
    leaves a cycle through another member than it entered by has the run of
    real edges between the two spliced in. An outside stub ends the path.
    """

    def rank(slug: str) -> tuple[int, str]:
        return _by_priority(rows[slug][0])

    own = [slug for slug in slugs if not rows[slug][1]]
    if not own:
        return []
    deepest = min(own, key=lambda slug: (-wave[slug], rank(slug)))
    if wave[deepest] == 0:
        return []
    path = [deepest]
    at = deepest
    while wave[at] > 0:
        depth = wave[at]
        members = [s for s, c in component_of.items() if c == component_of[at]]
        # Each candidate keeps the member it feeds, preferring `at` itself,
        # so a path leaves a cycle through another member only when it must.
        steps = [
            (source, member)
            for member in members
            for source in prev.get(member, [])
            if component_of[source] != component_of[at] and wave[source] == depth - 1
        ]
        if not steps:
            break
        source, member = min(steps, key=lambda s: (s[1] != at, rank(s[0])))
        if member != at:
            path[:0] = _within(member, at, next_, component_of)[:-1]
        path.insert(0, source)
        at = source
    return path


def _within(
    start: str,
    end: str,
    next_: Mapping[str, list[str]],
    component_of: Mapping[str, int],
) -> list[str]:
    """The shortest run of edges from ``start`` to ``end`` inside their
    component, both ends included. Two members of one strongly connected
    component always reach each other."""
    component = component_of[start]
    came_from: dict[str, str | None] = {start: None}
    queue = [start]
    head = 0
    while head < len(queue) and end not in came_from:
        at = queue[head]
        head += 1
        for target in next_.get(at, []):
            if component_of.get(target) == component and target not in came_from:
                came_from[target] = at
                queue.append(target)
    path: list[str] = []
    step: str | None = end
    while step is not None:
        path.insert(0, step)
        step = came_from[step]
    return path


def _disagreements(project: list[dict[str, Any]], ready: Sequence[Row]) -> list[str]:
    """Where ``task_ready`` and wave 0 disagree.

    A wave-0 task should be in ``task_ready`` unless someone holds a live claim
    on it, and nothing past wave 0 should be. Either direction means the reads
    saw the graph at different moments.
    """
    placed = {n["slug"] for n in project}
    found = []
    for node in project:
        held = node.get("status") == "in_progress" and not node.get("lease_expired")
        expected = node["wave"] == 0 and not node["cycle"] and not held
        if expected != node["ready"]:
            found.append(node["slug"])
    found.extend(t["slug"] for t in ready if t["slug"] not in placed)
    return found
