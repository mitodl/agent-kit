### Fixed

- A slug is now built from the title as the write-path scanner stores it, so
  PII redacted out of a title no longer survives in the slug (the node's
  permanent key). This covers `memory_store`, `task_create`,
  `workflow_project_create` and `task_comment`, whose slug comes from the
  comment body. Existing slugs are unchanged.
