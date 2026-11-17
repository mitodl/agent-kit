### Fixed

- A memory's `tags` are now scanned on `memory_store` and `memory_update`, and
  a Topic's slug is built from its name as the scanner stores it, so PII in a
  tag survives in neither the memory nor the `tp-` slug. Every topic lookup
  (`recall(topic=...)`, `memory_link` to a `name:kind` spec, the tag sync in
  `memory_update`, `witan migrate topics`) builds the slug the same way, so
  reads find what writes keyed. Names that redact to the same text now share
  one Topic (two email tags are both `tp-topic-redacted-email`), a tag whose
  scan fails gets no Topic, and a secret in a tag now refuses the write, as it
  does in a title. An update that omits `tags` links the Topics of the tags as
  stored, so a legacy tag the update redacts is still findable. Existing
  Topics are not re-keyed; an existing memory's tags are scanned the next time
  it is updated.

### Changed

- A Topic name (and so its slug) is scanned under the strictest policy across
  the base `[scan]` config and every `[scan.overlay]` table
  (`ScanConfig.strictest()`), since a Topic is shared across repos. One repo's
  overlay can now redact, and so merge, Topic names other repos share.
