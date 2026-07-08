"""Keycloak JWT → omnigraph per-user actor/token mapping (ADR 0004).

Two pure/lookup primitives for the deployed multi-user witan service:

- :func:`derive_actor_id` — deterministic ``sub`` → ``act-<id>`` mapping.
- :class:`ActorTokenResolver` — looks up the omnigraph bearer token
  pre-provisioned for an actor id. Never mints a token: omnigraph-server's
  bearer-token auth is static, read once at its own startup (see
  ``docs/adr/0004-keycloak-jwt-per-user-actor-mapping.md``), so the token has
  to already exist in the same source before it can be looked up here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_SANITIZE_RE = re.compile(r"[^a-z0-9-]+")


def derive_actor_id(sub: str) -> str:
    """Map a Keycloak ``sub`` claim to an omnigraph actor id.

    Lowercases, collapses any run of characters outside ``[a-z0-9-]`` to a
    single ``-``, and strips leading/trailing ``-``. ``sub`` is a UUID in
    practice, so this is close to identity — sanitizing defensively means a
    claim value never reaches a CLI arg or bearer-token lookup key unescaped.

    Raises ``ValueError`` for an empty or all-punctuation ``sub`` — an actor
    id of just ``act-`` would silently collide with every other such claim.
    """
    slug = _SANITIZE_RE.sub("-", sub.strip().lower()).strip("-")
    if not slug:
        raise ValueError(f"Cannot derive an actor id from sub={sub!r}")
    return f"act-{slug}"


class ActorTokenResolver:
    """Resolves ``act-<sub>`` → omnigraph bearer token from a provisioned map.

    Reads the JSON ``{actor_id: token}`` file at ``path`` — the same shape
    (and, in the deployed cluster, the same file) omnigraph-server itself
    reads via ``OMNIGRAPH_SERVER_BEARER_TOKENS_FILE``, so the provisioning
    pipeline maintains one artifact both processes agree on.

    The map is cached in memory and reloaded only when a requested actor id
    is missing from the current cache — not on a fixed TTL — so a
    newly-provisioned user succeeds on their first request without waiting
    for this process to restart, as long as their entry already exists on
    disk by the time they ask.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._cache: dict[str, str] = {}

    def resolve(self, actor_id: str) -> str:
        """Return the bearer token provisioned for ``actor_id``.

        Raises ``LookupError`` if no token exists for this actor even after a
        reload — the provisioning pipeline hasn't caught up, or the actor was
        never a member of ``witan-users``. Never falls back to a default
        identity.
        """
        if actor_id not in self._cache:
            self._load()
        if actor_id not in self._cache:
            raise LookupError(
                f"No omnigraph bearer token provisioned for actor {actor_id!r} "
                f"in {self.path}. The Keycloak→token provisioning pipeline may "
                "not have caught up yet, or this actor is not a witan-users member."
            )
        return self._cache[actor_id]

    def _load(self) -> None:
        try:
            raw = self.path.read_text()
        except FileNotFoundError as exc:
            raise ValueError(f"Actor token file not found: {self.path}") from exc
        except OSError as exc:
            raise ValueError(
                f"Failed to read actor token file {self.path}: {exc}"
            ) from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Failed to parse actor token file {self.path}: {exc}"
            ) from exc

        if not isinstance(parsed, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
        ):
            raise ValueError(
                f"Actor token file {self.path} must be a JSON object of "
                "{actor_id: token} string pairs."
            )

        self._cache = parsed
