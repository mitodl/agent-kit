### Fixed

- `task_claim` with a `session_id` renews a live claim held under the
  caller's own bare identity instead of refusing it as `held`, and records the
  session on it. `witan task run` claims before launch with no session id, so
  the launched agent could not renew its own claim the way the witan-task
  skill says to. The `witan task run` prompt now asks for the session id on
  renewal too.
