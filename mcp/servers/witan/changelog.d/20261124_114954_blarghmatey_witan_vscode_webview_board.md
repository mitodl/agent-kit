### Added

- `witan lsp` answers `witan.uiBundle` with the path of the web UI bundle this install ships, for an editor webview to load, and the council plugin declares its share of the web UI's reads so the host can relay them. A relayed read runs with prompts off, as the plugin's own tool calls do. Against a witan-core from before the relay it declares none of them and loads as before.
- The web UI reads through the editor when it runs in a VS Code webview (`src/webview.ts`), and over MCP to its own origin everywhere else. `src/mcp.ts` is still the one module that picks, and the views are unchanged.
- The bundle's scripts and stylesheets name the files they load relative to themselves; only `index.html` names the `/ui/` mount, so a webview can serve the bundle from its own path.
