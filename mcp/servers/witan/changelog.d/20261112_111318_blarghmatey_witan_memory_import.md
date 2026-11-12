### Added

- `witan memory import` reads Claude Code's per-project memory files
  (`~/.claude/projects/<project>/memory/*.md`) and reports each one's memory
  kind, the repo its project directory resolves to, and whether the graph
  already holds it. It writes nothing unless `--confirm` is passed with
  `--select` or `--all`, and every write goes through `memory_store`, so the
  secret and PII scan applies. `user` memories are skipped unless named
  exactly in `--select`, and a project directory that does not resolve to a
  single git checkout with a remote is reported as unresolved.
  `witan memory` is now a command group; `witan memory [QUERY]` still
  searches and lists as before.
