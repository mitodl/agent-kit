### Added

- A fan-out policy on workflow projects, the data a `witan runner` reads to
  decide where to claim: `fanout_enabled`, `fanout_concurrency` (the cap on
  runner-claimed tasks in flight across all runners, 1 when unset),
  `fanout_harnesses` (any of `claude`, `pi`, `codex`, `copilot`, `opencode`;
  unset allows any) and `fanout_labels` (labels a runner must advertise), with
  `fanout_set_by` recording who last changed it. `workflow_project_create`
  and `workflow_project_update` take all four, and `witan project create` /
  `update` take `--fanout/--no-fanout`, `--fanout-concurrency`,
  `--fanout-harness` and `--fanout-label`. Each field is written by its own
  mutation, so an edit to one cannot turn fan-out back on from a stale read.
- `workflow_project_list(fanout_enabled=True)` and `witan projects --fanout`
  list the active projects with fan-out on. `witan.fanout.FanoutPolicy` reads
  the policy off a project row and `admits` a runner's harnesses and labels;
  a project that never set the policy reads as disabled, so a runner never
  claims from it.

### Notes

- The schema change is additive (five optional `WorkflowProject`
  properties). A deployed graph needs the schema applied before the image
  that writes them.
