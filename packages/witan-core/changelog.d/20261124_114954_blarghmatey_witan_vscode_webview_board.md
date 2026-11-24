### Added

- The language server relays the web UI's reads for an editor webview: `witan.uiRead` runs one read on a folder's route, through the plugin that declares the tool, and `witan.uiTools` lists the reads that folder's routes serve with their arguments. Both refuse anything outside `witan_core.lsp.UI_READ_TOOLS`, ADR 0011's bound set, which joins `BOUND_TOOLS` (ADR 0012's 2026-11-24 amendment).
