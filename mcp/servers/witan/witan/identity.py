"""Keycloak JWT → omnigraph per-user actor/token lookup (ADR 0004).

:class:`ActorTokenResolver` looks up the omnigraph bearer token pre-provisioned
for an actor id. It never mints a token: omnigraph-server's bearer-token auth
is static, read once at its own startup (see
``docs/adr/0004-keycloak-jwt-per-user-actor-mapping.md``), so the token has to
already exist in the same source before it can be looked up here.

Both it and the ``sub`` → ``act-<id>`` mapping live in
:mod:`witan_core.identity`, shared with witan-code — which derives the same id
client-side to name the code-graph branch views it owns, and resolves the same
tokens server-side for the code-graph writes it now serves on witan's behalf
(:mod:`witan_code.ingest`). Both are re-exported here so this module remains
the one place to look for identity in witan.
"""

from __future__ import annotations

from witan_core.identity import ActorTokenResolver, derive_actor_id

__all__ = ["ActorTokenResolver", "derive_actor_id"]
