"""witan-code's client-side path into a deployed witan service (ADR 0005, path a).

The standalone ``witan-code`` CLI's read commands can dispatch over MCP to the
deployment that already serves this server's ``code_*`` tools (`witan serve`
mounts them with no prefix), authenticated with a per-user Keycloak JWT,
instead of opening the local ``~/.local/share/witan/code`` stores.

The mechanism lives in :mod:`witan_core.remote`; the two modules here bind
witan-code's policy — the ``witan-code login`` hint (:mod:`.oidc`) and which
tools are local-only plus how ``repo=None`` resolves (:mod:`.proxy`). Requires
the ``remote`` extra.
"""
