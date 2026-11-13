### Fixed

- A slug is now built from the title as the write-path scanner stores it, so
  PII redacted out of a title no longer survives in the slug (the node's
  permanent key). This covers `memory_store`, `task_create`,
  `workflow_project_create` and `task_comment`, whose slug comes from the
  comment body. Building the slug fails closed: when its scan cannot complete
  (a scanner raises under either `on_scanner_error` setting, or the write has
  no scan mapping), the slug is built from the placeholder `unscanned`
  instead of the raw text, and `witan.scan.slug_unscanned` is logged.
  Existing slugs are unchanged.
