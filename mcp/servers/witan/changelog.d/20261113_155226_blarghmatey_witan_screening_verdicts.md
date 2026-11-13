### Added

- A pluggable prompt-injection classifier slot. Name a provider (a
  `witan.classifiers` entry point or a `module:Attr` path) and its model in
  `[screen]` or `WITAN_SCREEN_PROVIDER`/`WITAN_SCREEN_MODEL`, and memory, task
  and task-comment writes are classified at the `change()` choke point, with
  the verdict, classifier version and a digest of the screened text stored on
  the record. witan ships no provider, so this is off by default. Classifier
  output is parsed fail-closed: anything but `{"decision":"clean"}` or
  `{"decision":"flagged"}` resolves to `flagged`. Long text is classified in
  chunks and stops at the first flag; text needing more than 16 classifier
  calls is stored flagged ("too long to screen") without one.
- `memory_get`, `task_get` (and each of its comments) and the memories a first
  `task_claim` surfaces carry a `screening` value. A record with no verdict for
  its current text reads as `unscreened` with a "[NOT security-screened ...]"
  notice. A flagged label names only reasons witan wrote itself; the
  classifier's own reason stays in `screen_reason` for operators. `witan task
  show` and the `witan run` launch prompt print the notices too.

### Notes

- Schema change: four nullable `screen_*` properties on `Memory`, `Task` and
  `TaskComment`, and the memory, task and comment insert/update mutations write
  them. A deployed graph needs `schema.pg` applied (the pulumi-omnigraph
  deploy) before this image rolls out, or every memory and task write fails
  on the unknown property. Local stores re-apply it on startup.
