### Added

- The `witan.memory.surfaced` log line carries `content_scores` and
  `title_scores`, parallel to `slugs`: the raw BM25 scores
  `WITAN_RANK_SURFACE_MIN_CONTENT` and `WITAN_RANK_SURFACE_MIN_TITLE` were
  applied to, `null` for a linked memory the title did not match. They are
  what lets the floors be set from production lines.
