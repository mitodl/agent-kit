### Added

- A language-server plugin can return a `Lens` with only `data` and fill in its title and command in `Plugin.resolve_lens`. The host sends it unresolved and resolves it on `codeLens/resolve`, so a lens that costs a read is only computed for the lines a client shows.
- For a client with no CodeLens (eglot; Zed by default), the host puts the resolved lenses on a hovered line into the hover and offers their commands as code actions, per ADR 0012's per-editor fallbacks.
- A plugin's `Context` has `folders` (every workspace folder) and `text(uri)` (the editor's buffer for an open document, else the file on disk).
- `witan_core.lsp.escape_markdown`, for rendering text a plugin does not control as plain text inside a hover.

### Changed

- The language server makes stdin read as an empty non-terminal while it runs, as it already did for stdout, so no prompt can read a JSON-RPC message as its answer, even when the server is started by hand in a terminal.
