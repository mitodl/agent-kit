# Changelog

All notable changes to the `witan` VS Code extension (`mitol.witan`) are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) (pre-1.0:
a MINOR bump may include breaking changes).

<!-- scriv-insert-here -->

## [0.1.0] - 2026-11-25

### Added

- The first release of the VS Code client for the witan language server
  (`mitol.witan`). It starts `witan lsp`, so hover, diagnostics, CodeLens,
  definition and references come from the server's council and code-graph
  plugins.
- A witan view listing, per workspace folder, your own in-progress tasks, the
  tasks linked to the checked-out branch, the repo's ready tasks and its
  active projects.
- Run in Terminal on a task or project runs the server's launch spec in a VS
  Code terminal: the agent gets the environment any terminal gets with
  `unset_env` removed and `env` set over it (never a strict env), and its
  argv exactly as the server wrote it. A spec of an unknown version is
  refused. A task launch whose agent cannot start releases the claim it took.
- Hand Off to Copilot and Hand Off to Claude Code, shown only when the target
  command exists, pass an unclaimed spec's prompt to that chat panel when the
  server marks the spec `handoff: true`.
- `witan.server.command` and `witan.agent` settings, both machine-scoped.

- A filter button in the witan view's title bar picks what the lists cover:
  this repo (the default), this repo plus work filed under no repo, every
  repo, or one project in any repo. The project picker offers the projects
  of every open folder's graph, and a picked project's work is listed once,
  through the folder it was picked from. The choice is kept per workspace and
  the view's description shows the scope the server applied. When lists or
  folders were answered in different scopes (an older deployment answers its
  task lists in this repo plus unfiled), the description lists each one and
  each section names its own next to its count.

- witan: Open Board (also a button on the Work view) opens the web UI's views (projects, board, waves, timeline, memory, graph and Bridge) in a panel for a workspace folder. The panel loads the bundle the installed witan ships and reads through the language server, so it holds no credential and makes no network request; its content security policy allows the bundle's own files and nothing else. It needs a witan whose language server has `witan.uiRead`, and says so when it does not.

- A project in the witan view expands to its open tasks by dependency wave
  ("Wave 0 (ready)", "Wave 1", …), each task expanding to its open blockers,
  including ones in another project. Run in Terminal and the Copilot and
  Claude handoffs are offered only on the ready ones. Needs a witan-council
  with `witan.listProjectTasks`.

### Changed

- A chat-panel handoff is also refused when VS Code's own environment sets `WITAN_TARGET`, `CLAUDE_PROJECT_DIR` or `WITAN_REPO`, even when the server offers it. The server can only check the environment it was started with, which `witan.server.command` can change (e.g. `["env", "-u", "WITAN_TARGET", "witan", "lsp"]`), while a chat-panel agent starts with VS Code's. The warning names the variables and still offers Run in Terminal.

### Fixed

- `witan.server.command` is documented as one argument per item, in the setting and the README, which had suggested adding `--target <name>` as written. When the server does not start, the error shows the command as the JSON array it is written as, and names any item that folds an option and its value together (e.g. `"--target production"`), instead of a space-joined line that hid the mistake.
