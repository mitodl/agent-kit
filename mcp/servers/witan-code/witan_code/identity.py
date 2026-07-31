"""Who this process writes as — the owner of the branch views it creates.

witan resolves an actor server-side, per request, from a validated JWT
(ADR 0004). witan-code cannot: indexing needs a git checkout, so the writer is
always a local process — a developer's machine, an agent, or the CI indexer —
talking to the shared cluster graph as itself. Its identity therefore comes
from the same place its *authorization* does, the OIDC session established by
``witan login`` (ADR 0005), and never from ``$USER``: a local username is not
the identity the cluster's bearer tokens and Cedar policies are written
against, and two people can share one.

The derived id names the branch views this process owns
(:mod:`witan_code.views`) and is what the write guard checks ownership
against, so it must agree byte-for-byte with the server-side derivation —
hence the shared :func:`witan_core.identity.derive_actor_id`.
"""

from __future__ import annotations

import os
import re

from witan_core.identity import ACTOR_PREFIX, derive_actor_id

from . import config as cfg_module

__all__ = ["ACTOR_ENV_VAR", "actor_id", "reset_cache"]

ACTOR_ENV_VAR = "WITAN_ACTOR"
"""Overrides the OIDC-derived identity.

For processes that hold a cluster token without an interactive login — the CI
indexer, a maintenance job — and as the seam tests set. Accepts either an
``act-…`` id verbatim or a raw ``sub`` to derive one from.
"""

_ACTOR_RE = re.compile(rf"^{ACTOR_PREFIX}[a-z0-9-]+$")

# Resolved once: unlike witan's per-request derivation, a witan-code process
# writes as exactly one identity for its lifetime. Logging in as someone else
# therefore needs a restart — which is also true of the branch views already
# written under the old id, so re-resolving mid-process would be the more
# surprising behavior.
_UNRESOLVED = object()
_cache: object = _UNRESOLVED


def actor_id(target: str | None = None) -> str | None:
    """The actor this process writes as, or ``None`` if it has no identity.

    ``None`` is the normal answer for purely local use: a local store has one
    user, who is its writer, and its view names stay un-namespaced. It is also
    the answer when a remote deployment is configured but nobody has logged in
    — the write guard turns that into a refusal with a login hint rather than
    letting an un-owned view land on the shared graph.

    Resolution order: :data:`ACTOR_ENV_VAR` > ``actor`` on the matched
    ``[targets.<name>]`` block > the ``sub`` of the cached OIDC token for the
    configured deployment > ``None``.
    """
    global _cache
    if _cache is _UNRESOLVED:
        _cache = _resolve(target)
    return _cache  # type: ignore[return-value]


def reset_cache() -> None:
    """Drop the memoized identity. For tests, and for ``witan login``'s process."""
    global _cache
    _cache = _UNRESOLVED


def _resolve(target: str | None) -> str | None:
    _, selected = cfg_module._select_target(target)
    explicit = os.environ.get(ACTOR_ENV_VAR) or (selected.actor if selected else None)
    if explicit:
        return _coerce(explicit)

    remote = cfg_module.load_remote_config(target)
    if remote is None:
        return None

    from witan_core.remote import oidc  # noqa: PLC0415 — httpx2 import isn't free

    sub = oidc.device_auth(remote, login_hint="witan login").cached_claims().get("sub")
    return derive_actor_id(sub) if isinstance(sub, str) and sub.strip() else None


def _coerce(value: str) -> str:
    """An explicit setting as an actor id, whether it was given as one or as a sub.

    A malformed ``act-…`` value raises rather than being re-derived: silently
    rewriting it would namespace this process's views under an id nobody
    authorized it for, and the only symptom would be writes that succeed
    locally and are refused by the cluster.
    """
    value = value.strip()
    if not value.startswith(ACTOR_PREFIX):
        return derive_actor_id(value)
    if not _ACTOR_RE.match(value):
        raise ValueError(
            f"{ACTOR_ENV_VAR}={value!r} is not a valid actor id "
            f"({ACTOR_PREFIX}<lowercase alphanumerics and dashes>). Set it to "
            "the raw OIDC `sub` instead to have the id derived."
        )
    return value
