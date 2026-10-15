### Added

- The witan-task skill has a checkpoint convention for long runs. After each durable step (branch, change, validated, pushed, pr-opened, review-requested, blocked) the holder leaves one `task_comment` line, `checkpoint <step>: <ref or exact command> | next: <action>`. A session that claims a task with checkpoints checks the last one against git and GitHub and resumes from the first step live state does not confirm, so a run cut off by a spend limit, network error or OOM kill can be picked up where it stopped.

### Changed

- The context hook's "New Comments on Work You Hold" block skips `checkpoint ` comments written by the task's holder, matched by person across sessions. Without this, every checkpoint an agent left came back on its own next prompt as a new comment to act on. A teammate's checkpoint on a task you hold still shows.
