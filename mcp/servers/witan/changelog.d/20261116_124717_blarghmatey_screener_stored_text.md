### Fixed

- `memory_store`, `task_create`, `workflow_project_create` and `task_comment`
  no longer fail with `AttributeError` when a screening classifier is
  configured. Building a slug asks the installed write guard for the text it
  will store, and neither the Screener nor the chain of a scan guard plus a
  Screener could answer.
