### Removed

- Breaking: `task_unlink` is gone. Pass `remove=True` to `task_link`, with the
  same `from_slug`, `to_slug` and `kind`, to remove a link. The result and the
  `blocked_by`/`parent_slug` resync are unchanged. `witan task unlink` still
  works and calls it.
- Breaking: `workflow_project_search` is gone. Pass `query` to
  `workflow_project_list` for the same BM25 search over title and description,
  ranked and capped at 20. Its `repo`, `status`, `phase` and `ready` filters
  apply to the matches.
- Breaking: `workflow_project_block`, `workflow_project_unblock` and
  `workflow_project_get_blockers` are gone. Call `workflow_project_update` on
  the blocked project with `add_blocked_by=[<blocker>]` or
  `remove_blocked_by=[<blocker>]`, the same shape as `add_repos`/`remove_repos`.
  A self-block now raises `ValueError` instead of returning `linked: false`.
  `workflow_project_get` gains a `blockers` list with each blocker's `slug`,
  `title`, `status` and `phase`, the fields `witan project show` read from
  `get_blockers`. `witan project block`/`unblock` keep their names.
