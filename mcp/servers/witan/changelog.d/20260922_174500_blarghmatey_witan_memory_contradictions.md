### Added

- **`memory_contradictions` lists every pair of memories that contradict each
  other.** No read returned that before: `recall` reports a pair only when both
  memories land in its ranked result, and `memory_neighbors` needs a slug to
  start from. Each row carries both endpoints and the link's
  `confidence`/`role`/`author`/`created_at`, one row per unordered pair (the
  newer link wins when a pair is stored both ways), newest first. A pair is in
  scope when either memory is in the requested repo. It joins ADR 0011's bound
  UI read set, amended to say so, and the UI read layer exposes it as
  `memoryContradictions` for the memory view's contradictions inbox.
