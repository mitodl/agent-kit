"""Server-declared cache directives for list results (MCP 2026-07-28, SEP-2549).

``tools/list``, ``prompts/list``, ``resources/list`` and ``resources/read`` now
carry ``ttlMs`` and ``cacheScope``, so a client can stop re-fetching a surface
that only changes on deploy. That is worth more here than for a typical small
server: between them the two witan servers publish ~50 tools, and every agent
session pulls the whole list.

FastMCP takes the hint as ``cache_ttl`` (seconds) / ``cache_scope`` on the
``FastMCP`` constructor and applies it uniformly to every cacheable result.
:func:`hint_kwargs` exists because the constructor only grew those arguments in
fastmcp 4.x, while both servers still pin ``fastmcp>=3.4.2,<5`` — on 3.4.x it
returns nothing and the servers emit no hint, exactly as before.

This module is NOT imported by ``witan_core/__init__`` — it depends on
``fastmcp`` (the ``mcp`` extra).
"""

from __future__ import annotations

import inspect
from typing import Any, Literal

from fastmcp import FastMCP

#: How long a client may reuse a list result, in seconds. The tool surface only
#: changes when a server is redeployed, so this trades a bounded window of
#: staleness after a deploy for not re-listing on every session.
DEFAULT_TTL_SECONDS = 300

CacheScope = Literal["public", "private"]


def hint_kwargs(
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    scope: CacheScope = "private",
) -> dict[str, Any]:
    """``FastMCP(**hint_kwargs())`` cache arguments, or ``{}`` on fastmcp 3.4.x.

    ``scope`` defaults to ``private`` — and should stay there for any server
    holding per-actor data. FastMCP's hint is uniform across every cacheable
    method, so ``public`` would also mark ``resources/read`` shareable, letting
    a shared cache serve one actor's read to another.
    """
    if "cache_ttl" not in inspect.signature(FastMCP.__init__).parameters:
        return {}
    return {"cache_ttl": ttl_seconds, "cache_scope": scope}
