### Fixed

- `concurrency-probe` no longer kills its workers before the server's mutate
  timeout expires, which had left committed rows in the shared graph that
  cleanup never saw. The worker timeout now defaults to the mutate timeout
  plus 30s, and a 30s settle runs before reconciling when any write failed.

### Added

- `concurrency-probe --worker-timeout` sets the worker deadline for a
  deployment whose `WITAN_REMOTE_WRITE_QUEUE_SECONDS` exceeds the default.
