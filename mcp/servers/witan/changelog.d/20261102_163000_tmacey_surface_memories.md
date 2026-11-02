### Added

- A first `task_claim` returns `memories`: up to three the task links to
  (`Addresses` or a shared `symbol_refs` entry) or its title closely matches,
  each with its author and an excerpt. `witan task run` puts that list in the
  prompt, labelled as teammates' notes, so a session reads what the team has
  learned without anyone deciding to look. A title match needs a hit in both the
  memory's title and its content, above raw BM25 floors
  (`WITAN_RANK_SURFACE_MIN_CONTENT`, `WITAN_RANK_SURFACE_MIN_TITLE`), because a
  task title as a plain search returns mostly noise.
- Every first claim logs `witan.memory.surfaced` with how many memories it
  carried, including none.

### Changed

- `recall(task=...)` also seeds from the task's title with the same match. It
  previously found nothing for a task with no links or `symbol_refs`. Memories
  linked by `Addresses` or `symbol_refs` now score above those matches on
  relevance, though recency, corroboration and confidence still count.
