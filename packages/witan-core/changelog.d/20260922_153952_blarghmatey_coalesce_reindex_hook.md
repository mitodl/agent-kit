### Changed

- **Optimistic-concurrency retries back off with full jitter.** The fixed
  `0.05 * attempt` schedule had no jitter and spent about 1.4s across all
  eight attempts, which is shorter than one served mutate, so a steady stream
  of writers could win every round against a slower one. Retries now sleep a
  random time up to `0.1 * 2**(attempt-1)`, capped at 1s.

- **An exhausted conflict retry raises `WriteContention`.** It is a
  `RuntimeError` and a `Refusal`, so existing `except RuntimeError` callers
  are unaffected, fastmcp logs it at WARNING instead of reporting a Sentry
  ERROR, and the caller is told nothing was written and the write can be
  retried.
