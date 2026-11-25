### Added

- The claim lease length is server configuration: `WITAN_CLAIM_LEASE_SECONDS`,
  or `[claims].lease_seconds` in config.toml, defaulting to 3600. A value that
  is not a positive whole number of seconds fails the server's start.
- A granted `task_claim` returns `lease_seconds`, the lease the server
  enforces, so a client renewing a lease schedules from the server's value.

### Changed

- `witan task run`'s prompt states the lease the claim reported instead of the
  launcher's own 60-minute constant, and `witan project run`'s prompt points
  the agent at `lease_seconds` in its own claim. A launcher talking to a
  deployment no longer tells its agent the wrong lease.
