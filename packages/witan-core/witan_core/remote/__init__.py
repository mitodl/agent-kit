"""Transport-agnostic client-side remote-access layer (the ``remote`` extra).

The generic mechanism a witan MCP server's CLI needs to talk to its *deployed*
self over ``streamable-http`` with a per-user OIDC identity (ADR-0005, path a),
factored out of witan-council so a second server (e.g. a deployed witan-code)
can reuse it instead of copy-pasting. On MCP 2026-07-28 that transport carries
no handshake and no session id, so every call stands alone (witan ADR-0009):

- :mod:`witan_core.remote.config` — :class:`~witan_core.remote.config.RemoteConfig`,
  which deployment to talk to and how to authenticate to it, resolved from the
  env vars / config.toml keys both servers share.
- :mod:`witan_core.remote.oidc` — :class:`~witan_core.remote.oidc.DeviceAuth`,
  the OIDC device-authorization grant (RFC 8628) plus a 0600 token cache.
- :mod:`witan_core.remote.proxy` — :class:`~witan_core.remote.proxy.RemoteMCPProxy`,
  a drop-in stand-in for an in-process server module that dispatches each tool
  call over MCP.

Both are parameterized: the caller binds server-specific policy (the token-cache
location and login hint; the admin-tool refusal set, repo resolution, and error
wording) via constructor args and subclass hooks. Nothing here imports ``witan``
or ``witan_code`` — the leaf invariant holds.
"""
