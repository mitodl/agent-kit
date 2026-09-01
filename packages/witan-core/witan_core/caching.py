"""Server-declared cache directives for list results (MCP 2026-07-28, SEP-2549).

``tools/list``, ``prompts/list``, ``resources/list`` and ``resources/read`` now
carry ``ttlMs`` and ``cacheScope``, so a client can stop re-fetching a surface
that only changes on deploy. That is worth more here than for a typical small
server: between them the two witan servers publish ~50 tools, and every agent
session pulls the whole list.

FastMCP takes the hint as ``cache_ttl`` (seconds) / ``cache_scope`` on the
``FastMCP`` constructor and applies it uniformly to every cacheable result.

This module is NOT imported by ``witan_core/__init__`` — it depends on
``fastmcp`` (the ``mcp`` extra).
"""

from __future__ import annotations

from typing import Any, Literal

#: How long a client may reuse a list result, in seconds. The tool surface only
#: changes when a server is redeployed, so this trades a bounded window of
#: staleness after a deploy for not re-listing on every session.
DEFAULT_TTL_SECONDS = 300

CacheScope = Literal["public", "private"]


def hint_kwargs(
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    scope: CacheScope = "private",
) -> dict[str, Any]:
    """``FastMCP(**hint_kwargs())`` cache arguments.

    ``scope`` defaults to ``private`` — and should stay there for any server
    holding per-actor data. FastMCP's hint is uniform across every cacheable
    method, so ``public`` would also mark ``resources/read`` shareable, letting
    a shared cache serve one actor's read to another.
    """
    return {"cache_ttl": ttl_seconds, "cache_scope": scope}
