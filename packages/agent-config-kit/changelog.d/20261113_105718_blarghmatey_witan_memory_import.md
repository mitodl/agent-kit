### Added

- `AgentPlatform.memory` describes where a platform's agent keeps the memory
  it writes for itself, as a read-only `MemoryStore` whose `entries()` returns
  `MemoryEntry` objects tagged with the project each belongs to and the
  directories that project can be. Claude Code's auto memory
  (`~/.claude/projects/<project>/memory/*.md`), Codex's memories
  (`$CODEX_HOME/memories/raw_memories.md`, one entry per session) and Copilot
  Chat's repository memory (VS Code `workspaceStorage`) are read; Pi and
  OpenCode keep no memory on disk and have `memory=None`.
- `pyyaml` is now a dependency, for reading memory frontmatter.
