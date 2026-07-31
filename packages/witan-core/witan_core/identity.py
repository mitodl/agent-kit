"""Keycloak ``sub`` → omnigraph actor id (ADR 0004), shared by both servers.

witan maps the claim server-side, off a validated JWT, to route a request to
the caller's omnigraph client. witan-code maps the *same* claim client-side,
off its cached OIDC token, to name the code-graph branch views it owns. One
function, because a view named for one derivation and authorized against
another is a bug with no symptom until two users collide on it.
"""

from __future__ import annotations

import re

_SANITIZE_RE = re.compile(r"[^a-z0-9-]+")

ACTOR_PREFIX = "act-"
"""Every derived actor id starts with this. Also what tells an actor id apart
from any other ``/``-separated component of a branch-view name."""


def derive_actor_id(sub: str) -> str:
    """Map a Keycloak ``sub`` claim to an omnigraph actor id.

    Lowercases, collapses any run of characters outside ``[a-z0-9-]`` to a
    single ``-``, and strips leading/trailing ``-``. ``sub`` is a UUID in
    practice, so this is close to identity — sanitizing defensively means a
    claim value never reaches a CLI arg, a bearer-token lookup key, or a
    branch-view name unescaped.

    Raises ``ValueError`` for a non-string ``sub``, or an empty/all-punctuation
    one — an actor id of just ``act-`` would silently collide with every other
    such claim.
    """
    if not isinstance(sub, str):
        raise ValueError(f"sub must be a string, got {type(sub).__name__}")
    slug = _SANITIZE_RE.sub("-", sub.strip().lower()).strip("-")
    if not slug:
        raise ValueError(f"Cannot derive an actor id from sub={sub!r}")
    return f"{ACTOR_PREFIX}{slug}"
