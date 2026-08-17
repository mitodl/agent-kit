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
#: Sync helpers that reach the store through one of the above.
BLOCKING_HELPERS = frozenset({"_store_memory", "_update_memory", "_update_task"})

SERVER = pathlib.Path(__file__).resolve().parent.parent / "witan" / "server.py"


def _blocking_call_name(node: ast.Call) -> str | None:
    """The store-reaching name this call resolves to, or None."""
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id == "client" and func.attr in BLOCKING_ATTRS:
            return f"client.{func.attr}"
    elif isinstance(func, ast.Name) and func.id in BLOCKING_HELPERS:
        return func.id
    return None


def _offenders() -> list[tuple[str, int, str]]:
    """(function, line, call) for every un-awaited store call in an async def."""
    tree = ast.parse(SERVER.read_text())
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
            name = _blocking_call_name(call)
            if name:
                found.append((fn.name, call.lineno, name))
    return found


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


def test_the_guard_can_actually_fail():
    """★ A structural test that cannot fail is worse than no test.

    This one is a pure AST walk over a file that is currently clean, so a
    mistake in the matching logic would look exactly like a pass. Feeding it a
    known-bad snippet proves the detector detects.
    """
    bad = ast.parse(
        "async def memory_store(ctx=None):\n"
        "    repo = await elicit.repo_or_detect(ctx, None)\n"
        "    rows = client.read('read.gq', 'get_memory', {'slug': 's'})\n"
        "    return _store_memory('lesson', 't', 'c')\n"
    )
    fn = bad.body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    awaited = {
        id(inner)
        for node in ast.walk(fn)
        if isinstance(node, ast.Await)
        for inner in ast.walk(node)
    }
    names = [
        _blocking_call_name(c)
        for c in ast.walk(fn)
        if isinstance(c, ast.Call) and id(c) not in awaited
    ]
    assert "client.read" in names
    assert "_store_memory" in names


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
