### Added

- The first release of the VS Code client for the witan language server
  (`mitol.witan`). It starts `witan lsp`, so hover, diagnostics, CodeLens,
  definition and references come from the server's council and code-graph
  plugins.
- A witan view listing, per workspace folder, your own in-progress tasks, the
  tasks linked to the checked-out branch, the repo's ready tasks and its
  active projects.
- Run in Terminal on a task or project runs the server's launch spec in a VS
  Code terminal: `unset_env` removed and `env` set over the environment any
  terminal gets, never a strict env. A spec of an unknown version is refused.
  A task launch whose agent cannot start releases the claim it took.
- Hand Off to Copilot and Hand Off to Claude Code, shown only when the target
  command exists, pass an unclaimed spec's prompt to that chat panel when the
  server marks the spec `handoff: true`.
- `witan.server.command` and `witan.agent` settings, both machine-scoped.
