### Added

- The `witan-project-tracker` skill has an "Advancing a Project" section for
  "advance the X project": a sequential loop that first reconciles in-progress
  tasks against their PRs' live state, then claims, implements, reviews and
  closes ready tasks one at a time. A task closes only on evidence checked in
  that session and quoted in its resolution (a merged PR, plus the release or
  rollout when the done-when needs one). A task whose done-when needs a human
  step stays open, and the loop stops with one numbered list of decisions,
  each with a recommendation.
