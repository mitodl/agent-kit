"""Keycloak ``sub`` → omnigraph actor id (ADR 0004), shared by both servers.

witan maps the claim server-side, off a validated JWT, to route a request to
the caller's omnigraph client. witan-code maps the *same* claim client-side,
off its cached OIDC token, to name the code-graph branch views it owns. One
function, because a view named for one derivation and authorized against
another is a bug with no symptom until two users collide on it.

:class:`ActorTokenResolver` is the other half of that mapping — actor id →
the omnigraph bearer token provisioned for it — and lives here for the same
reason: both servers resolve it, from the same file, in the same process when
``witan serve`` mounts witan-code's tools into witan's own server.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "ACTOR_PREFIX",
    "ActorTokenResolver",
    "derive_actor_handle",
    "derive_actor_id",
]

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


def derive_actor_handle(claims: Mapping[str, Any]) -> str | None:
    """A short, human-readable handle for the caller, or ``None`` if unknowable.

    Exists so a structured log line can be read without a second lookup: an
    ``act-<uuid>`` alone answers "which actor" but never "which person".

    ★ THE LOCAL-PART IS THE POINT, NOT AN ACCIDENT ★
    ``preferred_username`` is a full email address in this realm, and witan's
    own scanner classifies a bare email as ``pii`` (medium) — see
    ``witan.scan.detectors``. Logs ship to Loki, so this returns only the part
    before the ``@``: enough for a human reading a log line to recognise a
    colleague, without putting an RFC-5322 address into log storage
    (maintainer decision, 2026-08-07). The full value is deliberately still
    written to a node's ``author`` field, where it is the readable attribution
    ADR-0004 asks for; this is the narrower thing that goes to logs.

    Falls back to ``email`` for the same reason ``_current_author`` does — a
    token that carries one but not the other should not degrade to nothing.
    A claim that is not an email is returned as-is, so a realm that ever
    switches to bare usernames keeps working without a change here.
    """
    for claim in ("preferred_username", "email"):
        value = claims.get(claim)
        if not isinstance(value, str):
            continue
        handle = value.strip().split("@", 1)[0].strip()
        if handle:
            return handle
    return None


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
        reload — the provisioning pipeline hasn't caught up, or this identity
        is not one the pipeline provisions for. Never falls back to a default
        identity.
        """
        if actor_id not in self._cache:
            with self._lock:
                if actor_id not in self._cache:
                    self._load()
        if actor_id not in self._cache:
            # Deliberately does not name a `witan-users` Keycloak group as the
            # thing to check: there isn't one. The pipeline provisions every
            # enabled, non-service-account user of the Keycloak realm (ADR-0004
            # D3 addendum, 2026-08-05), so "am I in the group?" is the wrong
            # question to send an operator off to answer. The three causes below
            # are that contract's three ways of not being satisfied — a service
            # account is enabled and present, so omitting it would make this
            # message actively misleading for the one caller it fits.
            raise LookupError(
                f"No omnigraph bearer token provisioned for actor {actor_id!r} "
                f"in {self.path}. The Keycloak→token provisioning pipeline may "
                "not have caught up yet, or this account is disabled, absent "
                "from the Keycloak realm, or a service account (which the "
                "pipeline deliberately does not provision)."
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
