### Added

- `witan memory import` reads the memory coding agents keep on disk (Claude
  Code's auto memory, Codex's memories and Copilot Chat's repository memory,
  located through agent-config-kit's platform registry) and reports each
  memory's kind, the repo its project resolves to, and whether the graph
  already holds it. It reads every detected agent by default, `--harness`
  picks some, and an agent with no memory store, or an empty one, is named in
  the output. It writes nothing unless `--confirm` is passed with `--select`
  or `--all`, and every write goes through `memory_store`, so the secret and
  PII scan applies. `user` memories are skipped unless named exactly in
  `--select`, and a project that does not resolve to a single git checkout
  with a remote is reported as unresolved.
  `witan memory` is now a command group; `witan memory [QUERY]` still
  searches and lists as before.

### Changed

- witan-council requires agent-config-kit 0.15 or later.
