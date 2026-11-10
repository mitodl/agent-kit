### Changed

- `task_create`'s `similar` check searches every repo with the title plus the
  start of the description, and each hit carries its `repo`. Replayed against
  42 tasks closed as duplicates in production, the original reached the top 3
  for 40 of them, up from 36 with the title alone in the caller's repo.
