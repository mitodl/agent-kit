### Fixed

- `witan inject-context` against a deployed target now gives all of its reads
  one shared deadline, 30s from when the hook starts (`WITAN_CONTEXT_DEADLINE`
  overrides it). A read still unanswered at the deadline is cancelled, and the
  blocks that did answer render with a line naming what timed out. Before, a
  deployment that accepted a request and never answered held the hook for the
  MCP client's 300s read timeout, so the client killed it at its 45s hook
  timeout and nothing was injected. A partial block is never cached, so the
  next prompt retries the full read.
