### Added

- The language server host gives plugins `CommandContext.editor_environ` (and
  `Context.editor_environ`), the environment the editor started the server
  with. `serve()` drops `CLAUDE_PROJECT_DIR` and `WITAN_REPO` and sets
  `WITAN_TARGET` from `--target` before any plugin runs, so `os.environ` no
  longer shows what a chat-panel agent the editor starts would inherit.
