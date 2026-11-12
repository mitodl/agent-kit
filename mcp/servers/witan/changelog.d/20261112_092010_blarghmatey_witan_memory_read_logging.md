### Added

- The `witan.memory.surfaced` log line carries `content_scores` and
  `title_scores`, parallel to `slugs`: the raw BM25 scores
  `WITAN_RANK_SURFACE_MIN_CONTENT` and `WITAN_RANK_SURFACE_MIN_TITLE` were
  applied to, `null` for a linked memory the title did not match. They are
  what lets the floors be set from production lines.
- `memory_get` and `recall` log a `witan.memory.read` line in the same shape
  (`surface`, `task`, `session`, `count`, `slugs`), and `witan.memory.surfaced`
  gains `session`, so a Loki join on session (or `actor_id`) and slug says
  whether a surfaced memory was read. `session` is the 8-hex digest a claim
  holder carries after `#`, never the raw id. Both tools take an optional
  `session_id`, which only feeds the log; the CLI's remote proxy fills it in.
