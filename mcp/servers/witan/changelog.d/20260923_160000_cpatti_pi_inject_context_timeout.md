### Changed

- **The Pi workflow extension's context timeout is a named constant pinned to
  the Claude hook's.** `workflow-context.ts` already waited 45s, like the
  `witan inject-context` hook `witan setup` installs; both now read one named
  value (`INJECT_CONTEXT_TIMEOUT_SECONDS` in `witan.setup`,
  `INJECT_CONTEXT_TIMEOUT_MS` in the extension), and a test fails if they, or
  the `configs/pi/extensions` mirror, drift.
