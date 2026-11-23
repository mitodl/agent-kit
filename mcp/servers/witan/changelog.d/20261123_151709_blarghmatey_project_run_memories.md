### Added

- `witan project run` prompts carry a "Team memories" section: the memories a
  first `task_claim` would surface for each of the first five ready tasks the
  prompt lists, deduplicated and capped at three, each naming the task it
  matched. The lookup goes through `workflow_project_run_memories`, which is
  listed only to witan's CLI, and a failed lookup does not block the launch.
  Each run logs a `witan.memory.surfaced` line with `surface="project_run"`,
  the project, the tasks considered and matched, and the scores.
- A project launch sets a fresh `WITAN_CLIENT_INSTANCE` in the agent's
  environment and logs its surfacing under it, so the agent's later reads join
  to that line.

### Changed

- `witan.memory.surfaced` and `witan.memory.read` lines carry
  `client_instance`, a digest of the calling client process (the proxy's
  `X-Witan-Client-Instance` header, a handshake-era client's `Mcp-Session-Id`,
  or this process under local stdio). `session` falls back to it when the
  caller passes no `session_id`, so a `memory_get` through the proxy no
  longer logs a null session.
- `witan.memory.read` lines carry `bulk`, true from the 20th call to one read
  surface by one actor within a minute, so a scripted sweep can be left out of
  a measurement without filtering it by hand.
- Requires witan-core 0.61.0 for `witan_core.client_instance`.
