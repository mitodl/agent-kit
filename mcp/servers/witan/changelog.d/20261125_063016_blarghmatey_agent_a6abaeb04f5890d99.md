### Changed

- `witan lsp`'s `witan.runTask` (with `claim: false`) and `witan.runProject`
  can now return `handoff: true`, when the editor's environment carries none
  of `WITAN_TARGET`, `CLAUDE_PROJECT_DIR` or `WITAN_REPO`, the server was not
  started with `--target`, and the window has one folder. The check reads the
  editor's environment from witan-core's `CommandContext.editor_environ`
  rather than this process's, which `serve()` has already stripped, so the VS
  Code client offers its Copilot and Claude Code handoffs. Requires witan-core
  0.62.0 or later.
