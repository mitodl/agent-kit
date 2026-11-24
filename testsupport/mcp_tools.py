"""Call an MCP server's tools directly, without an MCP client.

No pytest here, so a seeding script run as its own process (the editor checks'
``seed.py``) calls the tools the same way the suites do.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any


def tool_fn(tool: Any) -> Callable[..., Any]:
    """A FastMCP tool as a plain callable; an ``async`` one runs to completion.

    Called directly rather than through an MCP client, a tool that takes a
    ``ctx: Context`` gets none and falls back to its non-interactive default.
    """
    fn = getattr(tool, "fn", tool)
    if inspect.iscoroutinefunction(fn):
        return lambda *a, **kw: asyncio.run(fn(*a, **kw))
    return fn
