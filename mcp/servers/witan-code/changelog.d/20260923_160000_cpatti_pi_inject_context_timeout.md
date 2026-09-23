### Fixed

- **The Pi extension now waits as long for the code-graph status block as
  Claude does.** `codegraph.ts`'s `before_agent_start` gave `witan-code
  inject-context` 5s, against the 15s the Claude `UserPromptSubmit` hook gets,
  so the ~10s cold store read on the first prompt of a cache window was killed
  every time: the cache never filled and Pi sessions got no block at all. Both
  now read one value (`INJECT_CONTEXT_TIMEOUT_SECONDS` in `witan_code.setup`,
  `INJECT_CONTEXT_TIMEOUT_MS` in the extension), and a test fails if they, or
  the `configs/pi/extensions` mirror, drift. Every other Pi handler stays
  detached, and a timeout still degrades to no context.
