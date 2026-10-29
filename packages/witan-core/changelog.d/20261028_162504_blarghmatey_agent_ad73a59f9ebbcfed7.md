### Fixed

- Manifest/Lance HEAD drift (`NEEDS_REPAIR`) is now terminal on the first
  attempt, with omnigraph's own message attached and a pointer to
  `omnigraph repair --json` for the diagnosis. The client used to shell out to
  `omnigraph repair --confirm --force` and retry, but neither 0.13 nor 0.14
  accepts those flags (their `repair` only diagnoses), so the call failed
  argument parsing, was discarded, and the retry met the same drift until the
  attempt budget ran out.

### Removed

- `OmnigraphClient._repair`, and the `env` keyword of `_with_retry_policy`
  and `_retry_loop`, which only `_repair` used.
