### Changed

- A chat-panel handoff is also refused when VS Code's own environment sets `WITAN_TARGET`, `CLAUDE_PROJECT_DIR` or `WITAN_REPO`, even when the server offers it. The server can only check the environment it was started with, which `witan.server.command` can change (e.g. `["env", "-u", "WITAN_TARGET", "witan", "lsp"]`), while a chat-panel agent starts with VS Code's. The warning names the variables and still offers Run in Terminal.
