"""Keycloak JWT → omnigraph per-user actor/token lookup (ADR 0004).

:class:`ActorTokenResolver` looks up the omnigraph bearer token pre-provisioned
for an actor id. It never mints a token: omnigraph-server's bearer-token auth
is static, read once at its own startup (see
``docs/adr/0004-keycloak-jwt-per-user-actor-mapping.md``), so the token has to
already exist in the same source before it can be looked up here.

The ``sub`` → ``act-<id>`` mapping itself lives in
:mod:`witan_core.identity`, shared with witan-code (which derives the same id
client-side to name the code-graph branch views it owns), and is re-exported
here so this module remains the one place to look for identity in witan.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from witan_core.identity import derive_actor_id

__all__ = ["ActorTokenResolver", "derive_actor_id"]


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
    disk by the time they ask. A reload re-parses the file only if its
    ``(mtime, size)`` changed since the last load, so a burst of misses for
    an unprovisioned/invalid actor costs a cheap ``stat()`` each time, not a
    repeated read-and-parse. FastMCP runs synchronous tool handlers in a
    thread pool, so ``resolve`` is called concurrently — a lock serializes
    the check-and-reload sequence.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._cache: dict[str, str] = {}
        self._lock = threading.Lock()
        self._last_stat: tuple[float, int] | None = None

    def resolve(self, actor_id: str) -> str:
        """Return the bearer token provisioned for ``actor_id``.

        Raises ``LookupError`` if no token exists for this actor even after a
        reload — the provisioning pipeline hasn't caught up, or the actor was
        never a member of ``witan-users``. Never falls back to a default
        identity.
        """
        if actor_id not in self._cache:
            with self._lock:
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
            stat = self.path.stat()
        except OSError as exc:
            raise ValueError(f"Actor token file not found: {self.path}") from exc

        current_stat = (stat.st_mtime, stat.st_size)
        if current_stat == self._last_stat:
            return

        try:
            raw = self.path.read_text(encoding="utf-8")
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
        self._last_stat = current_stat
