### Added

- `witan session-checkpoint` (the Stop hook) now closes a session it auto-closes
  with a summary built from what the session recorded: tasks in the project it
  claimed or closed, memories it stored, its branch and commits since the session started,
  and uncommitted files. That summary is what `workflow_project_status` shows
  as the next session's handoff, which used to be a fixed placeholder. A
  session with no activity gets a one-line "no recorded activity" summary, and
  the placeholder is kept only when a read failed and nothing else was found.
- `workflow_project_memories` takes `session`, a `ws-` slug, to return only the
  memories that one session produced in a single query.

### Fixed

- The Stop hook no longer overwrites a summary written with an explicit
  `workflow_session_end`. For a session started with `witan session start`
  against a deployed server, ending it with the `workflow_session_end` tool
  leaves the local handle in place, so the next Stop used to re-close the
  session with the placeholder. The hook now reads the session first and leaves an ended one
  alone.
