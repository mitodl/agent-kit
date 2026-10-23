### Added

- The `witan-project-tracker` skill has an "Advancing a Project" section for
  "advance the X project": a sequential loop that first reconciles your
  in-progress tasks against their PRs' live state, then claims, implements,
  reviews and opens a PR for ready tasks one at a time. Merging follows the
  autonomy envelope. A task closes only on evidence checked in that session
  and quoted in its resolution (a merged PR, plus the release or rollout when
  the done-when needs one). A task whose done-when needs a human step, or
  that hits a reserved decision, is held with a `checkpoint blocked:` comment
  while the loop works the rest, and the loop stops with one numbered list of
  decisions, each with a recommendation.
