"""No `async def` tool may call blocking store work on the event loop.

★ THIS IS A STRUCTURAL GUARD, AND IT EXISTS BECAUSE THE BUG WAS INVISIBLE TO
EVERY OTHER KIND OF TEST. Functional tests call tools directly and pass whether
or not the loop was blocked; the store returns the same rows either way. It only
showed up in production, as `/health` timing out under load.

The shape: FastMCP dispatches SYNC tool functions through a threadpool, so they
may block freely. A handful of witan's tools are `async def` — solely so they
can `await` an elicitation helper — and those run ON the event loop. A
synchronous `client.read`/`change`/`change_many` inside one stops the loop for
the length of an `omnigraph` subprocess, and nothing else gets scheduled.

Measured in QA on 2026-08-17 at 16 concurrent writers, before the fix: the pod
used 0.011 cores — 1.1% of one CPU, no limit set — while `/health` failed probes
at 5s AND 10s, readiness ejected the only replica, and APISIX returned 503 to
every caller while all 16 writes committed. A busy loop burns CPU; a loop that
cannot schedule a trivial coroutine for ten seconds at 1% CPU is blocked.

So: any new `async def` tool that reaches the store must route through
`server._offload`. This test fails with the offending function and line.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

#: Client methods that shell out to the `omnigraph` binary.
BLOCKING_ATTRS = frozenset(
    {"read", "change", "change_many", "load", "load_batch", "export"}
)

SERVER = pathlib.Path(__file__).resolve().parent.parent / "witan" / "server.py"


def _calls_client_directly(fn: ast.AST) -> bool:
    for call in ast.walk(fn):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "client"
            and func.attr in BLOCKING_ATTRS
        ):
            return True
    return False


def _plain_callees(fn: ast.AST) -> set[str]:
    return {
        call.func.id
        for call in ast.walk(fn)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }


def _store_reaching(tree: ast.Module) -> set[str]:
    """Every module-level function that reaches the store at ANY depth.

    ★ TRANSITIVE, AND THE FIRST VERSION OF THIS FILE WAS NOT — WHICH IS WHY IT
    PASSED WHILE FOUR CALL SITES STILL BLOCKED THE LOOP. It matched direct
    `client.*` calls plus a hardcoded three-name helper list, so
    `_resolve_topic_steps`, `_project_sessions`, `_unblock_dependents` and
    `_track_code_branch` were invisible to it: each reaches the store one or two
    frames down. `_unblock_dependents` is the worst of them — a listing read,
    a `get_task` per blocker, then a full read-modify-write per stale row, all
    on the event loop.

    A guard that reports clean on a file that still has the bug is worse than no
    guard, because it is trusted. A hand-maintained list of helper names will
    always drift behind the code; a closure over the call graph cannot.
    """
    funcs = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    reaching = {n for n, f in funcs.items() if _calls_client_directly(f)}
    changed = True
    while changed:  # fixed point; the graph is small and acyclic enough
        changed = False
        for name, fn in funcs.items():
            if name not in reaching and _plain_callees(fn) & reaching:
                reaching.add(name)
                changed = True
    return reaching


def _flag(tree: ast.Module) -> list[tuple[str, int, str]]:
    """(function, line, call) for every un-awaited store call in an async def."""
    reaching = _store_reaching(tree)
    found: list[tuple[str, int, str]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        # Anything lexically inside an `await` is offloaded; `_offload` is the
        # only thing these should be wrapped in, and it awaits.
        awaited = {
            id(inner)
            for node in ast.walk(fn)
            if isinstance(node, ast.Await)
            for inner in ast.walk(node)
        }
        for call in ast.walk(fn):
            if not isinstance(call, ast.Call) or id(call) in awaited:
                continue
            func = call.func
            name = None
            if isinstance(func, ast.Name) and func.id in reaching:
                name = func.id
            elif (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "client"
                and func.attr in BLOCKING_ATTRS
            ):
                name = f"client.{func.attr}"
            if name:
                found.append((fn.name, call.lineno, name))
    return found


def _offenders() -> list[tuple[str, int, str]]:
    """The real server's offenders."""
    return _flag(ast.parse(SERVER.read_text()))


def _names_flagged(tree: ast.Module) -> set[str]:
    """Just the called names, for the detector's own self-tests."""
    return {name for _fn, _line, name in _flag(tree)}


def test_no_async_tool_blocks_the_event_loop():
    offenders = _offenders()
    assert not offenders, "\n".join(
        [
            "These run ON the event loop and block it for the length of an "
            "omnigraph subprocess — route them through `await _offload(...)`:",
            *(
                f"  server.py:{line}  async def {fn}  ->  {call}()"
                for fn, line, call in offenders
            ),
        ]
    )


def test_the_guard_detects_a_directly_blocking_call():
    """★ A structural test that cannot fail is worse than no test.

    This is a pure AST walk over a file that is currently clean, so a mistake in
    the matching logic looks exactly like a pass. Feeding it known-bad input is
    the only thing that distinguishes "no offenders" from "no detector".
    """
    tree = ast.parse(
        "def _helper(s):\n"
        "    return client.read('read.gq', 'get_memory', {'slug': s})\n"
        "async def memory_store(ctx=None):\n"
        "    repo = await elicit.repo_or_detect(ctx, None)\n"
        "    return client.change_many([])\n"
    )
    assert "client.change_many" in _names_flagged(tree)


def test_the_guard_detects_blocking_reached_THROUGH_a_helper():
    """★ The case the first version of this guard missed entirely.

    `_resolve_topic_steps`, `_project_sessions`, `_unblock_dependents` and
    `_track_code_branch` all reach the store one or two frames down. A matcher
    keyed on direct `client.*` calls plus a hand-written helper list reported
    this file clean while all four blocked the loop — the guard was trusted and
    wrong, which is worse than absent.
    """
    tree = ast.parse(
        "def _deep(s):\n"
        "    return client.read('read.gq', 'q', {'slug': s})\n"
        "def _middle(s):\n"
        "    return _deep(s)\n"
        "async def task_claim(slug, ctx=None):\n"
        "    await elicit.confirm(ctx, 'x')\n"
        "    return _middle(slug)\n"
    )
    assert "_middle" in _names_flagged(tree), (
        "a helper two frames from the store must still be flagged"
    )


def test_the_guard_accepts_an_offloaded_call():
    """The converse: wrapping in `await _offload(...)` must clear it, or the
    guard would fail the very fix it exists to enforce."""
    tree = ast.parse(
        "def _deep(s):\n"
        "    return client.read('read.gq', 'q', {'slug': s})\n"
        "async def task_claim(slug, ctx=None):\n"
        "    return await _offload(_deep, slug)\n"
    )
    assert _names_flagged(tree) == set()


@pytest.mark.parametrize(
    "tool",
    [
        "memory_store",
        "memory_link",
        "workflow_project_advance",
        "workflow_project_complete",
        "task_create",
        "task_claim",
    ],
)
def test_the_known_async_tools_are_still_async_and_still_covered(tool):
    """Pins the set the guard is protecting.

    If one of these stops being `async def`, the guard silently stops covering
    it — it only inspects async functions. That would be fine (a sync tool goes
    to the threadpool and cannot block the loop), but it should be a deliberate
    change someone notices, not a silent narrowing of what is checked.
    """
    tree = ast.parse(SERVER.read_text())
    kinds = {
        node.name: type(node).__name__
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
    }
    assert kinds.get(tool) == "AsyncFunctionDef", (
        f"{tool} is no longer async — confirm it now runs in FastMCP's "
        "threadpool, then drop it from this list"
    )
