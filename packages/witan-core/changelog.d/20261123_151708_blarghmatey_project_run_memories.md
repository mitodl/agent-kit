### Added

- The remote proxy sends an `X-Witan-Client-Instance` header on every request,
  an id fixed for the proxy process (`witan_core.client_instance`), or the
  `WITAN_CLIENT_INSTANCE` a launcher set in the agent's environment. A
  deployed witan logs it on memory surfacing and read lines, so a read can be
  joined to the surfacing that preceded it when the agent passed no
  `session_id`. The proxy negotiates MCP 2026-07-28, which has no transport
  session to fall back on.
