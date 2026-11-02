### Fixed

- `recall` no longer takes minutes on a deployed graph. It read every expansion
  relation once per seed and every candidate once by slug, so a query that
  reached about 900 memories to return 5 made 1,128 omnigraph reads. Expansion and
  scoring now read `$slugs` lists in one round trip per relation, and `content`
  is read only for the memories being returned. Results and ranking are
  unchanged, except as noted under Changed.

### Changed

- `recall` and `memory_search` cap the total `content` in a result, so a default
  call fits in a tool response. The best-ranked memories stay whole. Past the
  budget a memory's `content` is cut to an excerpt and it gains
  `content_truncated` and `content_chars`; `memory_get` returns the full text.
