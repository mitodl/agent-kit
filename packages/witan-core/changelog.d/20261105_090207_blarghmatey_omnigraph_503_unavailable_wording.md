### Fixed

- `StoreUnavailable` no longer says witan "could not connect" when the server
  answered. A remote call that exhausts the unavailable budget on a refusal the
  server sent (an HTTP 503 over the pooled transport, an omnigraph 0.14 CLI exit
  75, or a 0.13 CLI reporting a graph that is still loading) now says the
  server answered but kept refusing the call as unavailable, so an operator
  checks the server's state instead of the network. A connect failure keeps the
  "could not connect" wording.
