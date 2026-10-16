### Changed

- The memory, task and project searches and listings in `read.gq` take their
  optional filters (`repo`, `kind`, `status`) as nullable parameters, so each
  read is one query instead of one per filter combination (36 queries become
  12). Against omnigraph 0.13.0 the collapsed queries returned the same rows
  in the same order, with the same limits and bm25 scores, for every filter
  combination, and timed within noise of the ones they replace. A caller
  running `read.gq` queries by name passes the filter key as `null` to leave
  it off: `search_all` is now `search_memories` with `repo` and `kind` null.
