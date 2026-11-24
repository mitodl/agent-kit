### Fixed

- `witan lsp` and `witan-code lsp` log at `WARNING` unless `WITAN_LOG_LEVEL`
  is set. They inherited the launcher's `INFO`, so VS Code showed every routine
  line as `[error]` (it labels a server's stderr that way) and pygls wrote each
  message body it sent, hover text and task titles included, to the editor's
  log files. `WITAN_LOG_LEVEL=info` or `debug` restores the verbose log; the
  bare `LOG_LEVEL` does not.
- The `httpx2` and `httpcore2` loggers join `httpx` and `httpcore` at
  `WARNING`. The mcp client and witan's remote proxy moved to the httpx2 fork,
  whose loggers are named for it, so every `witan` command against a
  deployment printed an `HTTP Request: POST ...` line for each request.
