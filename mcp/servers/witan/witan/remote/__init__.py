"""Client-side remote-access layer for the ``witan`` CLI (ADR 0005, path a).

Turns the umbrella CLI into an MCP client of the *deployed* witan service so
non-serve commands (``witan tasks``, ``witan memory search``, …) reach the
shared graph over ``streamable-http`` with a per-user Keycloak identity —
instead of the in-process static-token fallback that only works locally.

- :mod:`witan.remote.oidc` — OIDC device-authorization-grant login + token cache.
- :mod:`witan.remote.proxy` — :class:`RemoteServerProxy`, a drop-in stand-in
  for the ``witan.server`` module that dispatches each tool call over MCP.
"""
