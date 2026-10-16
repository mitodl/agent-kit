### Fixed

- `reindex-hook` reindexes files Codex edits. Codex's `Edit|Write` matcher selects its `apply_patch` tool, whose payload carries the patch text instead of a file path, so the hook would find nothing to index. It now reads the `*** Add File:`, `*** Update File:` and `*** Move to:` headers and reindexes each file the patch leaves on disk.
