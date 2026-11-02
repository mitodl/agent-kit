### Fixed

- `recall` no longer makes an omnigraph read per memory, which is what made it
  take minutes on a deployed graph. It read every expansion relation once per
  seed and every candidate once by slug, so a query that reached about 900
  memories to return 5 made 1,128 reads. Expansion and scoring now read `$slugs`
  lists, 200 slugs to a read, and `content` is read only for the memories being
  returned. Which memories rank where is unchanged.

### Changed

- `recall` and `memory_search` cap the total `content` in a result, so a default
  call fits in a tool response. The best-ranked memories stay whole. Past the
  budget a memory's `content` is cut to an excerpt and it gains
  `content_truncated` and `content_chars`; `memory_get` returns the full text.
