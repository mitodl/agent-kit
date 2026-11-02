### Added

- A first `task_claim` returns `memories`: up to three the task `Addresses` or
  its title closely matches, each with an excerpt. `witan task run` puts the same
  list in the prompt, so a session reads what the team has learned without
  anyone deciding to look. A title match needs a hit in both the memory's title
  and its content, above raw BM25 floors (`WITAN_RANK_SURFACE_MIN_CONTENT`,
  `WITAN_RANK_SURFACE_MIN_TITLE`), because a task title as a plain search returns
  mostly noise.
- Every first claim logs `witan.memory.surfaced` with how many memories it
  carried, including none.

### Changed

- `recall(task=...)` also seeds from the task's title with the same match, and
  ranks memories the task `Addresses` above those matches. It previously found
  nothing for a task with no links or `symbol_refs`.
